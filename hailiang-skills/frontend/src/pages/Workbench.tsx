import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  Archive,
  ArchiveRestore,
  Boxes,
  Braces,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  ChevronUp,
  CircleUserRound,
  CloudUpload,
  Code2,
  Download,
  FlaskConical,
  GitCompareArrows,
  History,
  Layers3,
  MoonStar,
  PackageCheck,
  Plus,
  RefreshCcw,
  Rocket,
  Save,
  Search,
  ShieldCheck,
  Sparkles,
  SunMedium,
  Trash2,
  Upload,
  UsersRound,
  Wrench,
  X,
} from "lucide-react";

import { getRuntimeWorkbenchApiBaseUrl } from "@/config/runtime";
import { MarkdownContent } from "@/components/MarkdownContent";
import { MessageBlocksRenderer } from "@/components/message-blocks/MessageBlocksRenderer";
import { TeamHandoffCard } from "@/components/message-blocks/TeamHandoffCard";
import { useChatStore } from "@/store/useChatStore";
import { presentationFromSseState } from "@/utils/conversationPresentation";
import type { FactFormField, MessageBlock } from "@/types/messageBlocks";
import type { MessageInteractionState, MessagePresentation, TeamHandoff } from "@/utils/api";
import type { SseV2State } from "@/types/streamEvents";
import {
  SkillFilesEditor,
  type EditableSkillFile,
} from "@/components/workbench/SkillFilesEditor";
import {
  readWorkbenchActor,
  storeWorkbenchActor,
  workbenchApi,
  type DependencyLock,
  type DebugHistoryItem,
  type Deployment,
  type EvaluationHistoryItem,
  type EvaluationRun,
  type ObjectRelease,
  type ObjectRevision,
  type RevisionTestSession,
  type SoulRevision,
  type StandardSkillConversion,
  type WorkbenchActor,
  type WorkbenchObjectImportResult,
  type WorkbenchObject,
  type WorkbenchObjectType,
} from "@/utils/workbenchApi";

type Section = "objects" | "versions" | "evaluation" | "convert" | "release";

const TYPE_META: Record<
  WorkbenchObjectType,
  { label: string; icon: typeof Wrench; tone: string; description: string }
> = {
  skill: {
    label: "Skill",
    icon: Wrench,
    tone: "text-sky-300",
    description: "原子业务能力与 Prompt",
  },
  expert: {
    label: "专家",
    icon: CircleUserRound,
    tone: "text-violet-300",
    description: "组合已发布 Skill 的业务专家",
  },
  expert_team: {
    label: "专家团",
    icon: UsersRound,
    tone: "text-amber-300",
    description: "多专家协同与主协调兜底",
  },
};

const emptyPayload = (type: WorkbenchObjectType): Record<string, unknown> => {
  if (type === "skill")
    return {
      prompt_markdown: "# Skill 规则\n\n请填写业务规则与输出要求。",
      runtime_contract: {},
      capability_ids: [],
    };
  if (type === "expert")
    return {
      rules_markdown: "# 专家规则\n\n请填写专家角色、边界和决策原则。",
      brief: "",
      budget: { max_iters: 4, max_skill_calls: 3 },
      capabilities: [
        "execute_skill",
        "request_declared_form",
        "read_effective_facts",
      ],
    };
  return {
    rules_markdown: "# 专家团规则\n\n请填写协同、转交与兜底原则。",
    brief: "",
    coordinator_expert_id: "",
    members: [],
  };
};

function shortHash(value: string | undefined): string {
  return value ? `${value.slice(0, 8)}…${value.slice(-5)}` : "--";
}

function formatTime(value: string | undefined): string {
  if (!value) return "--";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

type ReleaseGroup = {
  object_id: string;
  object_type: WorkbenchObjectType;
  object_key: string;
  name: string;
  releases: ObjectRelease[];
};

export function groupReleasesByObject(releaseItems: ObjectRelease[]): ReleaseGroup[] {
  const groups = new Map<string, ReleaseGroup>();
  for (const release of releaseItems) {
    const existing = groups.get(release.object_id);
    if (existing) {
      existing.releases.push(release);
      continue;
    }
    groups.set(release.object_id, {
      object_id: release.object_id,
      object_type: release.object_type,
      object_key: release.object_key,
      name: release.name,
      releases: [release],
    });
  }
  return [...groups.values()]
    .map((group) => ({
      ...group,
      releases: [...group.releases].sort(
        (left, right) => Number(right.is_current) - Number(left.is_current) || right.release_no - left.release_no,
      ),
    }))
    .sort((left, right) => left.name.localeCompare(right.name, "zh-CN") || left.object_key.localeCompare(right.object_key));
}

/**
 * Candidate-test stream IDs are browser-local correlation values. Some of the
 * embedded browsers used by the workbench expose Web Crypto but not
 * `randomUUID`, so do not make sending a test message depend on that optional
 * API.
 */
function makeCandidateStreamId(): string {
  const webCrypto = typeof globalThis !== "undefined" ? globalThis.crypto : undefined;
  if (typeof webCrypto?.randomUUID === "function") {
    return `candidate-stream-${webCrypto.randomUUID()}`;
  }
  return `candidate-stream-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`;
}

type CandidateTraceEntity = {
  object_id?: string;
  object_type?: string;
  object_key?: string;
  name?: string;
  revision_id?: string;
  revision_no?: number | null;
  release_id?: string | null;
};

type CandidateTurnDebug = {
  root?: CandidateTraceEntity | null;
  expert_team?: CandidateTraceEntity | null;
  expert?: CandidateTraceEntity | null;
  skill?: CandidateTraceEntity | null;
  references?: {
    used?: Array<{ path?: string; title?: string; source_type?: string; snippet?: string }>;
    available_but_not_used?: Array<{ path?: string; media_type?: string; content_hash?: string }>;
  };
  scripts?: Array<{
    path?: string;
    status?: string;
    input?: unknown;
    output?: unknown;
    error?: string;
    duration_ms?: number | null;
  }>;
  elapsed_ms?: number;
};

function debugValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  const rendered = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  return rendered.length > 6000 ? `${rendered.slice(0, 6000)}\n…（已截断）` : rendered;
}

function traceEntityLabel(entity: CandidateTraceEntity | null | undefined, empty = "本轮未经过") {
  if (!entity?.object_key) return empty;
  const revision = entity.revision_no ? ` · r${entity.revision_no}` : "";
  return `${entity.name || entity.object_key} (${entity.object_key})${revision}`;
}

function CandidateTurnTrace({ turn }: { turn: Record<string, unknown> }) {
  const debug = (turn.debug && typeof turn.debug === "object" ? turn.debug : {}) as CandidateTurnDebug;
  const events = Array.isArray(turn.events) ? turn.events as Array<Record<string, unknown>> : [];
  const references = debug.references?.used ?? [];
  const unusedReferences = debug.references?.available_but_not_used ?? [];
  const scripts = debug.scripts ?? [];
  return (
    <div className="mt-3 space-y-3 text-xs text-slate-300">
      <div className="grid gap-2 rounded-xl border border-white/10 bg-slate-950/50 p-3">
        <p><span className="text-slate-500">专家团：</span>{traceEntityLabel(debug.expert_team)}</p>
        <p><span className="text-slate-500">专家：</span>{traceEntityLabel(debug.expert)}</p>
        <p><span className="text-slate-500">实际 Skill：</span>{traceEntityLabel(debug.skill)}</p>
        <p><span className="text-slate-500">本轮耗时：</span>{typeof debug.elapsed_ms === "number" ? `${debug.elapsed_ms.toFixed(0)} ms` : "—"}</p>
      </div>

      <details className="rounded-xl border border-white/10 bg-slate-950/40 p-3">
        <summary className="cursor-pointer text-sky-200">引用资料 · 已使用 {references.length} 项</summary>
        <div className="mt-3 space-y-2">
          {references.length ? references.map((reference, index) => (
            <div key={`${reference.path}-${index}`} className="rounded-lg border border-emerald-400/15 bg-emerald-400/[0.04] p-2">
              <p className="break-all text-emerald-100">{reference.title || reference.path}</p>
              {reference.path && reference.title ? <p className="mt-1 break-all text-slate-500">{reference.path}</p> : null}
              {reference.snippet ? <pre className="mt-2 max-h-32 overflow-auto whitespace-pre-wrap text-slate-400">{reference.snippet}</pre> : null}
            </div>
          )) : <p className="text-slate-500">本轮没有实际注入或检索引用资料。</p>}
          {unusedReferences.length ? <details className="rounded-lg border border-white/10 p-2 text-slate-500"><summary className="cursor-pointer">候选修订内可用但本轮未引用的资料（{unusedReferences.length}）</summary><ul className="mt-2 space-y-1 break-all">{unusedReferences.map((reference, index) => <li key={`${reference.path}-${index}`}>{reference.path}</li>)}</ul></details> : null}
        </div>
      </details>

      <details className="rounded-xl border border-white/10 bg-slate-950/40 p-3">
        <summary className="cursor-pointer text-sky-200">Skill 脚本执行 · {scripts.length} 项</summary>
        <div className="mt-3 space-y-2">
          {scripts.length ? scripts.map((script, index) => (
            <details key={`${script.path}-${index}`} className="rounded-lg border border-white/10 p-2">
              <summary className="cursor-pointer break-all"><span className={script.status === "success" ? "text-emerald-200" : script.status === "failed" ? "text-rose-200" : "text-amber-200"}>{script.status === "success" ? "成功" : script.status === "failed" ? "失败" : "未执行"}</span><span className="ml-2 font-mono text-slate-300">{script.path || "未命名脚本"}</span>{typeof script.duration_ms === "number" ? <span className="ml-2 text-slate-500">{script.duration_ms} ms</span> : null}</summary>
              <div className="mt-3 grid gap-2">
                <label className="text-slate-500">输入<pre className="mt-1 max-h-36 overflow-auto whitespace-pre-wrap rounded bg-black/20 p-2 text-slate-300">{debugValue(script.input)}</pre></label>
                <label className="text-slate-500">输出<pre className="mt-1 max-h-36 overflow-auto whitespace-pre-wrap rounded bg-black/20 p-2 text-slate-300">{debugValue(script.output)}</pre></label>
                {script.error ? <p className="text-amber-200">说明：{script.error}</p> : null}
              </div>
            </details>
          )) : <p className="text-slate-500">当前 Skill 没有候选脚本。</p>}
        </div>
      </details>

      <details className="rounded-xl border border-white/10 bg-slate-950/40 p-3">
        <summary className="cursor-pointer text-sky-200">原始运行事件 · {events.length} 条</summary>
        <div className="mt-3 max-h-64 space-y-2 overflow-auto">
          {events.map((event, index) => {
            const payload = event.payload && typeof event.payload === "object" ? event.payload as Record<string, unknown> : {};
            return <details key={`${String(event.event_id ?? event.event_type)}-${index}`} className="rounded-lg border border-white/10 p-2"><summary className="cursor-pointer font-mono text-sky-200">{String(event.event_type ?? "event")}</summary><pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap text-slate-400">{debugValue(payload)}</pre></details>;
          })}
        </div>
      </details>
    </div>
  );
}

function StatusBadge({
  children,
  tone = "slate",
}: {
  children: React.ReactNode;
  tone?: "slate" | "green" | "amber" | "blue";
}) {
  const tones = {
    slate: "border-slate-700 bg-slate-800/70 text-slate-300",
    green: "border-emerald-400/25 bg-emerald-400/10 text-emerald-200",
    amber: "border-amber-400/25 bg-amber-400/10 text-amber-200",
    blue: "border-sky-400/25 bg-sky-400/10 text-sky-200",
  };
  return (
    <span
      className={`inline-flex rounded-full border px-2.5 py-1 text-[11px] font-medium ${tones[tone]}`}
    >
      {children}
    </span>
  );
}

export default function Workbench() {
  const apiBaseUrl = getRuntimeWorkbenchApiBaseUrl();
  const { themeMode, setThemeMode } = useChatStore();
  const [actor, setActor] = useState<WorkbenchActor | null>(() =>
    readWorkbenchActor(),
  );
  const [actorName, setActorName] = useState("");
  const [section, setSection] = useState<Section>("objects");
  const [objects, setObjects] = useState<WorkbenchObject[]>([]);
  const [releases, setReleases] = useState<ObjectRelease[]>([]);
  const [deployments, setDeployments] = useState<Deployment[]>([]);
  const [audit, setAudit] = useState<
    Array<{
      event_id: string;
      action: string;
      actor_id: string;
      created_at: string;
    }>
  >([]);
  const [selectedId, setSelectedId] = useState<string>("");
  const [selectedObject, setSelectedObject] = useState<WorkbenchObject | null>(
    null,
  );
  const [payload, setPayload] = useState<Record<string, unknown>>({});
  const [changeSummary, setChangeSummary] = useState("");
  const [runtimeContractText, setRuntimeContractText] = useState("{}");
  const [selectedReleaseIds, setSelectedReleaseIds] = useState<string[]>([]);
  const [expandedDependencyObjectIds, setExpandedDependencyObjectIds] = useState<string[]>([]);
  const [expandedPublishedObjectIds, setExpandedPublishedObjectIds] = useState<string[]>([]);
  const [search, setSearch] = useState("");
  const [typeFilter, setTypeFilter] = useState<WorkbenchObjectType | "all">(
    "all",
  );
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{
    tone: "ok" | "error";
    text: string;
  } | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [showArchived, setShowArchived] = useState(false);
  const [archiveTarget, setArchiveTarget] = useState<WorkbenchObject | null>(null);
  const [archiveConfirmation, setArchiveConfirmation] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<WorkbenchObject | null>(
    null,
  );
  const [deleteConfirmation, setDeleteConfirmation] = useState("");
  const [idMigrationTarget, setIdMigrationTarget] = useState<WorkbenchObject | null>(null);
  const [idMigrationConfirmation, setIdMigrationConfirmation] = useState("");
  const [nextObjectKey, setNextObjectKey] = useState("");
  const [referenceMigrationTarget, setReferenceMigrationTarget] = useState<WorkbenchObject | null>(null);
  const [referenceMigrationConfirmation, setReferenceMigrationConfirmation] = useState("");
  const [referenceFromReleaseId, setReferenceFromReleaseId] = useState("");
  const [referenceToReleaseId, setReferenceToReleaseId] = useState("");
  const [deactivateDeploymentTarget, setDeactivateDeploymentTarget] = useState<Deployment | null>(null);
  const [deactivateTeamConfirmation, setDeactivateTeamConfirmation] = useState("");
  const [newObject, setNewObject] = useState({
    object_type: "skill" as WorkbenchObjectType,
    object_key: "",
    name: "",
    description: "",
  });

  useEffect(() => {
    document.body.dataset.theme = themeMode;
    document.documentElement.dataset.theme = themeMode;
  }, [themeMode]);
  const [debugEvidenceId, setDebugEvidenceId] = useState("");
  const [debugComplete, setDebugComplete] = useState(false);
  const [manualConfirmed, setManualConfirmed] = useState(false);
  const [testRevisionId, setTestRevisionId] = useState("");
  const [debugObjectId, setDebugObjectId] = useState("");
  const [debugObject, setDebugObject] = useState<WorkbenchObject | null>(null);
  const [debugTypeFilter, setDebugTypeFilter] = useState<WorkbenchObjectType | "all">("all");
  const [debugSearch, setDebugSearch] = useState("");
  const [debugReleaseId, setDebugReleaseId] = useState("");
  const [debugHistory, setDebugHistory] = useState<DebugHistoryItem[]>([]);
  const [evaluationHistory, setEvaluationHistory] = useState<EvaluationHistoryItem[]>([]);
  const [historyStatus, setHistoryStatus] = useState<"all" | "active" | "completed">("all");
  const [soulRevisions, setSoulRevisions] = useState<SoulRevision[]>([]);
  const [soulRevisionId, setSoulRevisionId] = useState("");
  const [soulDraft, setSoulDraft] = useState("");
  const [conversion, setConversion] = useState<StandardSkillConversion | null>(null);
  const [conversionDraft, setConversionDraft] = useState<Record<string, unknown> | null>(null);
  const [conversionTargetId, setConversionTargetId] = useState("");
  const debugTargetInitialized = useRef(false);
  const candidateStreamAbortRef = useRef<AbortController | null>(null);
  const candidateLastSeqRef = useRef<Record<string, number>>({});
  const [revisionTestSession, setRevisionTestSession] =
    useState<RevisionTestSession | null>(null);
  const [revisionTestInput, setRevisionTestInput] = useState("");
  const [candidateTargetExpertId, setCandidateTargetExpertId] = useState("");
  const [candidateConversationState, setCandidateConversationState] = useState<SseV2State | null>(null);
  const candidateTeamName = candidateConversationState?.expert.team.name || "未选择";
  const candidateExpertName = candidateConversationState?.expert.active.name || "未选择";
  const candidateActiveSkill = candidateConversationState?.session.active_skill;
  // expert_direct is an execution-source marker, not a Skill selection. The
  // title intentionally contains the Expert's name, so rendering it here as
  // a Skill would make a direct Expert reply look like an invisible Skill run.
  const candidateSkillName = candidateActiveSkill?.skill_id === "expert_direct"
    ? "未选择"
    : candidateActiveSkill?.title || "未选择";
  const [evaluationInputs, setEvaluationInputs] = useState(
    "我想了解适合自己的升学路径\n请根据当前信息给出下一步建议",
  );
  const [evaluationRun, setEvaluationRun] = useState<EvaluationRun | null>(
    null,
  );
  const [pendingAssets, setPendingAssets] = useState<EditableSkillFile[]>([]);

  const loadAll = useCallback(async () => {
    try {
      const [objectResult, releaseResult, deploymentResult, auditResult] =
        await Promise.all([
          workbenchApi.listObjects(apiBaseUrl, showArchived),
          workbenchApi.listReleases(apiBaseUrl),
          workbenchApi.listDeployments(apiBaseUrl),
          workbenchApi.listAudit(apiBaseUrl),
        ]);
      setObjects(objectResult.objects);
      setReleases(releaseResult.releases);
      setDeployments(deploymentResult.deployments);
      setAudit(auditResult.events);
      const soulResult = await workbenchApi.listSoulRevisions(apiBaseUrl);
      setSoulRevisions(soulResult.revisions);
    } catch (error) {
      setNotice({
        tone: "error",
        text: error instanceof Error ? error.message : "工作台数据加载失败",
      });
    }
  }, [apiBaseUrl, showArchived]);

  useEffect(() => {
    void loadAll();
  }, [loadAll]);

  const openObject = useCallback(
    async (objectId: string) => {
      setBusy(true);
      try {
        const detail = await workbenchApi.getObject(apiBaseUrl, objectId);
        setSelectedId(objectId);
        setSelectedObject(detail);
        const latest = detail.revisions?.[0];
        const revisionAssets = latest
          ? await workbenchApi.listRevisionAssets(
              apiBaseUrl,
              latest.revision_id,
            )
          : { assets: [] };
        const nextPayload = latest?.payload ?? emptyPayload(detail.object_type);
        setPayload(nextPayload);
        setRuntimeContractText(
          JSON.stringify(nextPayload.runtime_contract ?? {}, null, 2),
        );
        setSelectedReleaseIds(
          (latest?.dependency_locks ?? []).map((item) => item.release_id),
        );
        setDebugEvidenceId("");
        setDebugComplete(false);
        setManualConfirmed(false);
        setEvaluationRun(null);
        setPendingAssets(
          revisionAssets.assets.map((asset) => ({
            relative_path: asset.relative_path,
            media_type: asset.media_type,
            content_base64: asset.content_base64,
            size: asset.size_bytes,
          })),
        );
        setSection("objects");
      } catch (error) {
        setNotice({
          tone: "error",
          text: error instanceof Error ? error.message : "对象加载失败",
        });
      } finally {
        setBusy(false);
      }
    },
    [apiBaseUrl],
  );

  const filteredObjects = useMemo(() => {
    const term = search.trim().toLowerCase();
    return objects.filter(
      (item) =>
        (typeFilter === "all" || item.object_type === typeFilter) &&
        (!term ||
          `${item.name} ${item.object_key} ${item.description}`
            .toLowerCase()
            .includes(term)),
    );
  }, [objects, search, typeFilter]);

  const dependencyReleases = useMemo(() => {
    if (!selectedObject || selectedObject.object_type === "skill") return [];
    const expected =
      selectedObject.object_type === "expert" ? "skill" : "expert";
    return releases.filter(
      (item) => item.object_type === expected && !item.archived,
    ).sort((left, right) => Number(right.is_current) - Number(left.is_current));
  }, [releases, selectedObject]);

  const dependencyReleaseGroups = useMemo(
    () => groupReleasesByObject(dependencyReleases),
    [dependencyReleases],
  );

  const publishedReleaseGroups = useMemo(
    () => groupReleasesByObject(releases),
    [releases],
  );

  const selectedLocks = useMemo<DependencyLock[]>(
    () =>
      dependencyReleases
        .filter((item) => selectedReleaseIds.includes(item.release_id))
        .map((item) => ({
          object_id: item.object_id,
          object_type: item.object_type,
          object_key: item.object_key,
          release_id: item.release_id,
          release_no: item.release_no,
          content_hash: item.content_hash,
        })),
    [dependencyReleases, selectedReleaseIds],
  );

  const duplicateDependencyObjectIds = useMemo(() => {
    const counts = new Map<string, number>();
    for (const lock of selectedLocks) {
      counts.set(lock.object_id, (counts.get(lock.object_id) ?? 0) + 1);
    }
    return [...counts.entries()]
      .filter(([, count]) => count > 1)
      .map(([objectId]) => objectId);
  }, [selectedLocks]);

  const uniqueSelectedLocks = useMemo(() => {
    const seen = new Set<string>();
    return selectedLocks.filter((lock) => {
      if (seen.has(lock.object_id)) return false;
      seen.add(lock.object_id);
      return true;
    });
  }, [selectedLocks]);

  // Team membership is stored on the revision payload, while its concrete
  // Expert releases live in dependency locks. Keep the presentation/routing
  // fields beside the member instead of dropping them every time a revision
  // is saved.
  const teamMemberSettings = useMemo(() => {
    const savedMembers = Array.isArray(payload.members)
      ? payload.members.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object")
      : [];
    return uniqueSelectedLocks.map((lock) => {
      const saved = savedMembers.find((item) => String(item.expert_id ?? "") === lock.object_key) ?? {};
      const release = dependencyReleases.find((item) => item.release_id === lock.release_id);
      return {
        expert_id: lock.object_key,
        mention_name: String(saved.mention_name ?? release?.name ?? lock.object_key),
        routing_brief: String(saved.routing_brief ?? ""),
        name: release?.name ?? lock.object_key,
      };
    });
  }, [dependencyReleases, payload.members, uniqueSelectedLocks]);

  function updateTeamMember(expertId: string, patch: Partial<{ mention_name: string; routing_brief: string }>) {
    setPayload({
      ...payload,
      members: teamMemberSettings.map((member) =>
        member.expert_id === expertId ? { ...member, ...patch } : member,
      ),
    });
  }

  function selectDependencyRelease(objectId: string, releaseId: string | null) {
    setSelectedReleaseIds((current) => {
      const retained = current.filter(
        (id) => dependencyReleases.find((release) => release.release_id === id)?.object_id !== objectId,
      );
      return releaseId ? [...retained, releaseId] : retained;
    });
  }

  const latestRevision = selectedObject?.revisions?.[0] ?? null;
  const latestRelease = selectedObject?.releases?.[0] ?? null;
  const selectedTestRevision =
    debugObject?.revisions?.find(
      (revision) => revision.revision_id === testRevisionId,
    ) ?? null;
  const selectedTestRelease = debugObject?.releases?.find(
    (release) => release.release_id === debugReleaseId && release.revision_id === selectedTestRevision?.revision_id,
  ) ?? null;
  const selectedSoulRevision = soulRevisions.find((item) => item.soul_revision_id === soulRevisionId) ?? null;
  const latestCandidateAssistant = revisionTestSession?.transcript.at(-1)?.role === "assistant"
    ? revisionTestSession.transcript.at(-1)
    : null;
  const activeCandidateForm = Boolean(
    latestCandidateAssistant?.blocks?.some((block) => {
      if (block.type !== "fact_form") return false;
      const formId = String((block.payload as Record<string, unknown>)?.form_id ?? "");
      return latestCandidateAssistant.interaction_states?.[`fact_form:${formId}`]?.status === "active";
    }),
  );
  const activeCandidateHandoff = latestCandidateAssistant?.interaction_states?.team_handoff?.status === "active";
  const candidateTeamMembers = useMemo(() => {
    if (debugObject?.object_type !== "expert_team" || !selectedTestRevision) return [];
    const coordinatorObjectId = String(selectedTestRevision.payload.coordinator_expert_id ?? "");
    const configuredMembers = Array.isArray(selectedTestRevision.payload.members)
      ? selectedTestRevision.payload.members.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object")
      : [];
    return selectedTestRevision.dependency_locks
      .filter((item) => item.object_type === "expert")
      .map((item) => {
        const configured = configuredMembers.find((member) => String(member.expert_id ?? "") === item.object_key);
        return {
          expert_id: item.object_key,
          mention_name: String(configured?.mention_name ?? objects.find((object) => object.object_id === item.object_id)?.name ?? item.object_key),
          routing_brief: String(configured?.routing_brief ?? ""),
          is_coordinator: item.object_id === coordinatorObjectId,
        };
      });
  }, [debugObject?.object_type, objects, selectedTestRevision]);
  const candidateTargetExpert = candidateTeamMembers.find((item) => item.expert_id === candidateTargetExpertId) ?? null;

  function chooseTestRevision(revisionId: string) {
    setTestRevisionId(revisionId);
    setRevisionTestSession(null);
    setRevisionTestInput("");
    setCandidateTargetExpertId("");
    setCandidateConversationState(null);
    candidateLastSeqRef.current = {};
    setEvaluationRun(null);
    setDebugEvidenceId("");
    setDebugComplete(false);
    setManualConfirmed(false);
    const release = debugObject?.releases?.find((item) => item.revision_id === revisionId);
    setDebugReleaseId(release?.release_id ?? "");
    syncDebugTargetUrl(debugObjectId, revisionId, release?.release_id ?? "");
  }

  function chooseHistoryEvidence(evidenceId: string) {
    setDebugEvidenceId(evidenceId);
    setDebugComplete(true);
    setManualConfirmed(false);
    setNotice({ tone: "ok", text: "已选中该修订的历史测试证据，请完成发布确认。" });
  }

  function downloadEvidence(kind: "debug" | "evaluation", item: DebugHistoryItem | EvaluationHistoryItem) {
    const payload = JSON.stringify({ exported_at: new Date().toISOString(), evidence_type: kind, ...item }, null, 2);
    const url = URL.createObjectURL(new Blob([payload], { type: "application/json" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = `${kind}-${"debug_session_id" in item ? item.debug_session_id : item.run_id}.json`;
    link.click();
    URL.revokeObjectURL(url);
  }

  function downloadTranscript(
    revisionId: string,
    transcript: RevisionTestSession["transcript"],
    trace: RevisionTestSession["trace"],
    sessionId: string,
    debugSessionId: string,
  ) {
    const outputDiagnostics = trace.flatMap((turn) => {
      const events = Array.isArray(turn.events) ? turn.events : [];
      return events
        .filter((event) => event && typeof event === "object")
        .map((event) => event as Record<string, unknown>)
        .filter((event) => ["model_output_completion", "model_output_truncated"].includes(String(event.event_type ?? "")))
        .map((event) => ({
          turn: turn.turn ?? null,
          event_type: event.event_type,
          timestamp: event.timestamp ?? event.created_at ?? null,
          ...(event.payload && typeof event.payload === "object" ? event.payload as Record<string, unknown> : {}),
        }));
    });
    const contextArchiveEvents = trace.flatMap((turn) => {
      const events = Array.isArray(turn.events) ? turn.events : [];
      return events
        .filter((event) => event && typeof event === "object")
        .map((event) => event as Record<string, unknown>)
        .filter((event) => [
          "profile_candidate_archived",
          "questionnaire_context_archived",
          "form_abandoned",
        ].includes(String(event.event_type ?? "")))
        .map((event) => ({
          turn: turn.turn ?? null,
          event_type: event.event_type,
          timestamp: event.timestamp ?? event.created_at ?? null,
          ...(event.payload && typeof event.payload === "object" ? event.payload as Record<string, unknown> : {}),
        }));
    });
    const url = URL.createObjectURL(new Blob([JSON.stringify({
      revision_id: revisionId,
      debug_session_id: debugSessionId,
      // This is the exact session identifier accepted by
      // /api/v1/operations/diagnostics/sessions/query.
      session_id: sessionId,
      messages: transcript,
      // No response content is duplicated here. These records say whether the
      // provider reported a terminal reason and, when confirmed, why output
      // was cut so the same session can be looked up in diagnostics.
      output_diagnostics: outputDiagnostics,
      context_archive_events: contextArchiveEvents,
    }, null, 2)], { type: "application/json" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = `candidate-conversation-${revisionId}.json`;
    link.click();
    URL.revokeObjectURL(url);
  }

  async function saveSoulRevision() {
    if (!actor || !soulDraft.trim()) return;
    setBusy(true);
    try {
      const saved = await workbenchApi.saveSoulRevision(apiBaseUrl, { content: soulDraft, actor_id: actor.actor_id });
      setSoulRevisions((items) => [saved, ...items]);
      setSoulRevisionId(saved.soul_revision_id);
      setSoulDraft("");
      setNotice({ tone: "ok", text: `Soul r${saved.revision_no} 已保存，可用于本次测试。` });
    } catch (error) {
      setNotice({ tone: "error", text: error instanceof Error ? error.message : "Soul 保存失败" });
    } finally { setBusy(false); }
  }

  async function previewStandardSkill(file: File) {
    if (!actor) return;
    setBusy(true);
    try {
      const result = await workbenchApi.previewStandardSkillConversion(apiBaseUrl, file, actor.actor_id);
      setConversion(result);
      setConversionDraft(result.draft);
      setConversionTargetId("");
      setNotice({ tone: "ok", text: "已完成静态解析并生成转换草稿，请确认后保存为候选修订。" });
    } catch (error) { setNotice({ tone: "error", text: error instanceof Error ? error.message : "转换预览失败" }); }
    finally { setBusy(false); }
  }

  async function commitStandardSkill() {
    if (!actor || !conversionDraft) return;
    setBusy(true);
    try {
      const saved = await workbenchApi.commitStandardSkillConversion(apiBaseUrl, { draft: conversionDraft, target_object_id: conversionTargetId || null, actor_id: actor.actor_id });
      await loadAll();
      await openObject(saved.object_id);
      setConversion(null);
      setConversionDraft(null);
      setNotice({ tone: "ok", text: `已保存为候选 r${saved.revision.revision_no}，请进入调试与用例验证。` });
    } catch (error) { setNotice({ tone: "error", text: error instanceof Error ? error.message : "保存转换草稿失败" }); }
    finally { setBusy(false); }
  }

  function syncDebugTargetUrl(objectId: string, revisionId: string, releaseId: string) {
    const params = new URLSearchParams(window.location.search);
    if (objectId) params.set("object_id", objectId); else params.delete("object_id");
    if (revisionId) params.set("revision_id", revisionId); else params.delete("revision_id");
    if (releaseId) params.set("release_id", releaseId); else params.delete("release_id");
    const query = params.toString();
    window.history.replaceState(null, "", `${window.location.pathname}${query ? `?${query}` : ""}`);
  }

  const loadDebugHistory = useCallback(async (objectId: string, revisionId: string, status = historyStatus) => {
    const filters = { object_id: objectId, revision_id: revisionId, status: status === "all" ? undefined : status };
    const [debugResult, evaluationResult] = await Promise.all([
      workbenchApi.listDebugSessions(apiBaseUrl, filters),
      workbenchApi.listEvaluationRuns(apiBaseUrl, filters),
    ]);
    setDebugHistory(debugResult.sessions);
    setEvaluationHistory(evaluationResult.runs);
  }, [apiBaseUrl, historyStatus]);

  const loadDebugTarget = useCallback(async (
    objectId: string,
    requestedRevisionId?: string,
    requestedReleaseId?: string,
  ) => {
    const detail = await workbenchApi.getObject(apiBaseUrl, objectId);
    const requested = detail.revisions?.find((item) => item.revision_id === requestedRevisionId);
    const revision = requested ?? detail.revisions?.find((item) => item.validation.valid) ?? null;
    const release = detail.releases?.find(
      (item) => item.release_id === requestedReleaseId && item.revision_id === revision?.revision_id,
    ) ?? detail.releases?.find((item) => item.revision_id === revision?.revision_id) ?? null;
    setDebugObjectId(detail.object_id);
    setDebugObject(detail);
    setTestRevisionId(revision?.revision_id ?? "");
    setDebugReleaseId(release?.release_id ?? "");
    setRevisionTestSession(null);
    setRevisionTestInput("");
    setEvaluationRun(null);
    setDebugEvidenceId("");
    setDebugComplete(false);
    setManualConfirmed(false);
    await loadDebugHistory(detail.object_id, revision?.revision_id ?? "");
    syncDebugTargetUrl(detail.object_id, revision?.revision_id ?? "", release?.release_id ?? "");
  }, [apiBaseUrl, loadDebugHistory]);

  useEffect(() => {
    if (debugTargetInitialized.current || !objects.length) return;
    debugTargetInitialized.current = true;
    const params = new URLSearchParams(window.location.search);
    const requestedObjectId = params.get("object_id") || objects[0].object_id;
    void loadDebugTarget(
      requestedObjectId,
      params.get("revision_id") || undefined,
      params.get("release_id") || undefined,
    ).catch(() => void loadDebugTarget(objects[0].object_id));
  }, [objects, loadDebugTarget]);

  const filteredDebugObjects = useMemo(() => {
    const term = debugSearch.trim().toLowerCase();
    return objects.filter((item) =>
      (debugTypeFilter === "all" || item.object_type === debugTypeFilter) &&
      (!term || `${item.name} ${item.object_key}`.toLowerCase().includes(term)),
    );
  }, [objects, debugSearch, debugTypeFilter]);

  async function registerActor() {
    if (!actorName.trim()) return;
    setBusy(true);
    try {
      const value = await workbenchApi.registerActor(
        apiBaseUrl,
        actorName.trim(),
        actor?.device_token,
      );
      storeWorkbenchActor(value);
      setActor(value);
    } catch (error) {
      setNotice({
        tone: "error",
        text: error instanceof Error ? error.message : "用户名登记失败",
      });
    } finally {
      setBusy(false);
    }
  }

  async function createObject() {
    if (!actor) return;
    setBusy(true);
    try {
      const created = await workbenchApi.createObject(apiBaseUrl, {
        ...newObject,
        actor_id: actor.actor_id,
      });
      setShowCreate(false);
      setNewObject({
        object_type: "skill",
        object_key: "",
        name: "",
        description: "",
      });
      await loadAll();
      await openObject(created.object_id);
      setNotice({
        tone: "ok",
        text: `${created.name} 已创建，请保存第一个修订。`,
      });
    } catch (error) {
      setNotice({
        tone: "error",
        text: error instanceof Error ? error.message : "创建失败",
      });
    } finally {
      setBusy(false);
    }
  }

  async function permanentlyDeleteObject() {
    if (!actor || !deleteTarget || deleteConfirmation !== deleteTarget.name)
      return;
    setBusy(true);
    try {
      const deleted = await workbenchApi.deleteObject(
        apiBaseUrl,
        deleteTarget.object_id,
        {
          confirmation_name: deleteConfirmation,
          actor_id: actor.actor_id,
        },
      );
      setDeleteTarget(null);
      setDeleteConfirmation("");
      setSelectedId("");
      setSelectedObject(null);
      setPayload({});
      setPendingAssets([]);
      setSection("objects");
      await loadAll();
      setNotice({
        tone: "ok",
        text: `“${deleted.name}”已永久删除，共清理 ${deleted.revision_count} 个修订、${deleted.release_count} 个发布版本和 ${deleted.asset_count} 个文件。`,
      });
    } catch (error) {
      setNotice({
        tone: "error",
        text: error instanceof Error ? error.message : "永久删除失败",
      });
    } finally {
      setBusy(false);
    }
  }

  async function archiveObject() {
    if (!actor || !archiveTarget || archiveConfirmation !== archiveTarget.name) return;
    setBusy(true);
    try {
      await workbenchApi.archiveObject(apiBaseUrl, archiveTarget.object_id, actor.actor_id);
      const archivedName = archiveTarget.name;
      setArchiveTarget(null);
      setArchiveConfirmation("");
      setSelectedId("");
      setSelectedObject(null);
      setPayload({});
      setPendingAssets([]);
      await loadAll();
      setNotice({ tone: "ok", text: `“${archivedName}”已归档，历史修订和发布版本均已保留。` });
    } catch (error) {
      setNotice({ tone: "error", text: error instanceof Error ? error.message : "归档失败" });
    } finally {
      setBusy(false);
    }
  }

  async function unarchiveObject(target: WorkbenchObject) {
    if (!actor) return;
    setBusy(true);
    try {
      await workbenchApi.unarchiveObject(apiBaseUrl, target.object_id, actor.actor_id);
      await loadAll();
      await openObject(target.object_id);
      setNotice({ tone: "ok", text: `“${target.name}”已恢复到对象库。` });
    } catch (error) {
      setNotice({ tone: "error", text: error instanceof Error ? error.message : "恢复归档失败" });
    } finally {
      setBusy(false);
    }
  }

  async function migrateObjectId() {
    if (!actor || !idMigrationTarget || idMigrationConfirmation !== idMigrationTarget.name || !nextObjectKey.trim()) return;
    setBusy(true);
    try {
      const result = await workbenchApi.migrateObjectId(apiBaseUrl, idMigrationTarget.object_id, {
        new_object_key: nextObjectKey.trim(), confirmation_name: idMigrationConfirmation, actor_id: actor.actor_id,
      });
      setIdMigrationTarget(null);
      setIdMigrationConfirmation("");
      setNextObjectKey("");
      await loadAll();
      await openObject(result.successor.object_id);
      setNotice({
        tone: "ok",
        text: `已创建新 ID “${result.successor.object_key}”的待调试修订；旧对象仍保留。${result.downstream.length ? ` 发布后可迁移 ${result.downstream.length} 个直接引用对象。` : ""}`,
      });
    } catch (error) {
      setNotice({ tone: "error", text: error instanceof Error ? error.message : "修改 ID 失败" });
    } finally {
      setBusy(false);
    }
  }

  async function migrateReferences() {
    if (!actor || !referenceMigrationTarget || referenceMigrationConfirmation !== referenceMigrationTarget.name || !referenceFromReleaseId || !referenceToReleaseId) return;
    setBusy(true);
    try {
      const result = await workbenchApi.migrateReferences(apiBaseUrl, {
        from_release_id: referenceFromReleaseId, to_release_id: referenceToReleaseId,
        confirmation_name: referenceMigrationConfirmation, actor_id: actor.actor_id,
      });
      setReferenceMigrationTarget(null);
      setReferenceMigrationConfirmation("");
      await loadAll();
      setNotice({ tone: "ok", text: result.created.length ? `已生成 ${result.created.length} 个引用迁移草稿，请逐个调试并发布。` : "没有当前修订引用所选旧版本。" });
    } catch (error) {
      setNotice({ tone: "error", text: error instanceof Error ? error.message : "迁移引用失败" });
    } finally {
      setBusy(false);
    }
  }

  async function saveRevision() {
    if (!actor || !selectedObject) return;
    if (duplicateDependencyObjectIds.length) {
      setNotice({
        tone: "error",
        text: "同一依赖对象锁定了多个版本，请在展开的对象版本列表中保留一个版本后再保存。",
      });
      return;
    }
    let nextPayload = { ...payload };
    if (selectedObject.object_type === "skill") {
      try {
        nextPayload = {
          ...nextPayload,
          runtime_contract: JSON.parse(runtimeContractText),
        };
      } catch {
        setNotice({
          tone: "error",
          text: "Runtime Contract 必须是合法 JSON。",
        });
        return;
      }
    }
    if (selectedObject.object_type === "expert_team") {
      nextPayload = {
        ...nextPayload,
        members: teamMemberSettings.map(({ expert_id, mention_name, routing_brief }) => ({
          expert_id,
          mention_name: mention_name.trim(),
          routing_brief: routing_brief.trim(),
        })),
      };
    }
    setBusy(true);
    try {
      const saved = await workbenchApi.saveRevision(
        apiBaseUrl,
        selectedObject.object_id,
        {
          base_revision_id: latestRevision?.revision_id ?? null,
          payload: nextPayload,
          dependency_locks: selectedLocks.map((item) => ({
            release_id: item.release_id,
          })),
          assets: pendingAssets.map(
            ({ relative_path, media_type, content_base64 }) => ({
              relative_path,
              media_type,
              content_base64,
            }),
          ),
          change_summary: changeSummary,
          actor_id: actor.actor_id,
        },
      );
      await loadAll();
      await openObject(selectedObject.object_id);
      setChangeSummary("");
      setNotice({
        tone: saved.validation.valid ? "ok" : "error",
        text: saved.validation.valid
          ? `r${saved.revision_no} 已保存且校验通过。`
          : (saved.validation.errors ?? []).join("；"),
      });
    } catch (error) {
      setNotice({
        tone: "error",
        text: error instanceof Error ? error.message : "保存失败",
      });
    } finally {
      setBusy(false);
    }
  }

  function questionnaireConfigFromSkill(): Record<string, unknown> | null {
    try {
      const configuration = payload.configuration && typeof payload.configuration === "object"
        ? payload.configuration as Record<string, unknown>
        : {};
      let questionnaire = configuration.questionnaire && typeof configuration.questionnaire === "object"
        ? configuration.questionnaire
        : null;
      if (!questionnaire) {
        try {
          const contract = JSON.parse(runtimeContractText) as Record<string, unknown>;
          questionnaire = contract.questionnaire && typeof contract.questionnaire === "object"
            ? contract.questionnaire
            : null; // legacy revision compatibility
        } catch {
          questionnaire = null;
        }
      }
      const config = questionnaire && typeof questionnaire === "object"
        ? (questionnaire as Record<string, unknown>).config_json
        : null;
      if (config && typeof config === "object") return config as Record<string, unknown>;
      const configPath = questionnaire && typeof questionnaire === "object"
        ? String((questionnaire as Record<string, unknown>).config_path || "assets/questionnaire.json")
        : "assets/questionnaire.json";
      const asset = pendingAssets.find((item) => item.relative_path === configPath);
      if (!asset) return null;
      const binary = atob(asset.content_base64);
      const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
      const parsed = JSON.parse(new TextDecoder().decode(bytes));
      return parsed && typeof parsed === "object" ? parsed as Record<string, unknown> : null;
    } catch {
      return null;
    }
  }

  function exportQuestionnaireConfig() {
    const config = questionnaireConfigFromSkill();
    if (!config) {
      setNotice({ tone: "error", text: "当前 Skill 没有有效的 assets/questionnaire.json。" });
      return;
    }
    const blob = new Blob([JSON.stringify(config, null, 2)], { type: "application/json" });
    const anchor = document.createElement("a");
    anchor.href = URL.createObjectURL(blob);
    anchor.download = "questionnaire.config.json";
    anchor.click();
    URL.revokeObjectURL(anchor.href);
  }

  function importQuestionnaireConfig(file: File | undefined) {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      try {
        const config = JSON.parse(String(reader.result || ""));
        const configuration = payload.configuration && typeof payload.configuration === "object"
          ? payload.configuration as Record<string, unknown>
          : {};
        const questionnaire: Record<string, unknown> = configuration.questionnaire && typeof configuration.questionnaire === "object"
          ? { ...(configuration.questionnaire as Record<string, unknown>) }
          : { enabled: true };
        delete questionnaire.config_json;
        questionnaire.config_path = "assets/questionnaire.json";
        const encoded = new TextEncoder().encode(JSON.stringify(config, null, 2));
        let binary = "";
        for (let index = 0; index < encoded.length; index += 0x8000) binary += String.fromCharCode(...encoded.subarray(index, index + 0x8000));
        setPendingAssets((current) => [
          ...current.filter((item) => item.relative_path !== "assets/questionnaire.json"),
          { relative_path: "assets/questionnaire.json", media_type: "application/json", content_base64: btoa(binary), size: encoded.length },
        ]);
        setPayload({ ...payload, configuration: { ...configuration, questionnaire } });
        setNotice({ tone: "ok", text: "问卷配置已导入，请预览并保存为新修订。" });
      } catch {
        setNotice({ tone: "error", text: "问卷配置必须是合法 JSON。" });
      }
    };
    reader.readAsText(file);
  }

  async function addAssets(files: FileList | null, kind: "reference" | "asset") {
    if (!files) return;
    const next = await Promise.all(
      Array.from(files).map(async (file) => {
        const buffer = await file.arrayBuffer();
        const bytes = new Uint8Array(buffer);
        let binary = "";
        for (let index = 0; index < bytes.length; index += 0x8000) {
          binary += String.fromCharCode(
            ...bytes.subarray(index, index + 0x8000),
          );
        }
        return {
          relative_path: `${kind === "asset" ? "assets" : "references"}/${file.name}`,
          media_type: file.type || "application/octet-stream",
          content_base64: btoa(binary),
          size: file.size,
        };
      }),
    );
    setPendingAssets((current) => [
      ...current.filter(
        (existing) =>
          !next.some((item) => item.relative_path === existing.relative_path),
      ),
      ...next,
    ]);
  }

  async function runRevisionTestTurn(
    submittedInput?: string,
    formSubmission?: { source_message_id: string; form_id: string; values: Record<string, unknown> },
    teamHandoffSelection?: { source_message_id: string; handoff_id: string; target_expert_id: string; team_id?: string; mention_name?: string },
    expertSelection?: { target_expert_id: string },
  ) {
    const message = (submittedInput ?? revisionTestInput).trim();
    if (!actor || !selectedTestRevision || (!message && !formSubmission && !teamHandoffSelection && !expertSelection)) return;
    setBusy(true);
    let streamAbortController: AbortController | null = null;
    try {
      const session =
        revisionTestSession?.revision_id === selectedTestRevision.revision_id &&
        revisionTestSession.status === "active"
          ? revisionTestSession
          : await workbenchApi.createRevisionTestSession(apiBaseUrl, {
              revision_id: selectedTestRevision.revision_id,
              soul_revision_id: soulRevisionId || null,
              actor_id: actor.actor_id,
            });
      const submittedAt = new Date().toISOString();
      const visibleUserMessage = teamHandoffSelection
        ? `@${teamHandoffSelection.mention_name || teamHandoffSelection.target_expert_id}`
        : expertSelection
          ? `@${candidateTeamMembers.find((item) => item.expert_id === expertSelection.target_expert_id)?.mention_name || expertSelection.target_expert_id} ${message}`
        : formSubmission
          ? "已提交表单"
          : message;
      const optimisticAssistantId = makeCandidateStreamId();
      streamAbortController = new AbortController();
      candidateStreamAbortRef.current = streamAbortController;
      let streamError = "";
      setRevisionTestSession({
        ...session,
        transcript: [
          ...(session.transcript ?? []),
          { role: "user", content: visibleUserMessage, created_at: submittedAt },
          { role: "assistant", content: "", message_id: optimisticAssistantId, blocks: [], interaction_states: {}, created_at: submittedAt },
        ],
      });
      await workbenchApi.streamRevisionTestTurn(
        apiBaseUrl,
        session.debug_session_id,
        {
          user_message: message,
          form_submission: formSubmission ?? null,
          team_handoff_selection: teamHandoffSelection ?? null,
          expert_selection: expertSelection ?? null,
          actor_id: actor.actor_id,
        },
        (event, payload) => {
          if (event === "state" && payload.protocol === "hailiang.sse.v2") {
            const state = payload as unknown as SseV2State;
            const lastSeq = candidateLastSeqRef.current[state.run_id] ?? -1;
            if (state.seq <= lastSeq) return;
            candidateLastSeqRef.current[state.run_id] = state.seq;
            setCandidateConversationState(state);
            setRevisionTestSession((current) => current && current.debug_session_id === session.debug_session_id ? {
              ...current,
              transcript: current.transcript.map((item) => item.message_id === optimisticAssistantId
                ? {
                    ...item,
                    message_id: state.message_id ?? item.message_id,
                    content: state.assistant.content,
                    presentation: presentationFromSseState(state),
                    team_handoff:
                      "candidates" in state.team_handoff && Array.isArray(state.team_handoff.candidates)
                        ? state.team_handoff as TeamHandoff
                        : item.team_handoff,
                  }
                : item),
            } : current);
            return;
          }
          if (event === "reply_delta") {
            const delta = String(payload.delta ?? "");
            if (!delta) return;
            setRevisionTestSession((current) => current && current.debug_session_id === session.debug_session_id ? {
              ...current,
              transcript: current.transcript.map((item) => item.message_id === optimisticAssistantId
                ? { ...item, content: `${item.content}${delta}` }
                : item),
            } : current);
            return;
          }
          if (event === "message") {
            const record = payload.assistant_message as RevisionTestSession["transcript"][number] | undefined;
            if (!record) return;
            setRevisionTestSession((current) => current && current.debug_session_id === session.debug_session_id ? {
              ...current,
              transcript: current.transcript.map((item) => item.message_id === optimisticAssistantId ? record : item),
            } : current);
            return;
          }
          if (event === "done") {
            const result = payload as unknown as { debug_session?: RevisionTestSession };
            if (result.debug_session) setRevisionTestSession(result.debug_session);
            return;
          }
          if (event === "error") {
            const errorMessage = String(payload.message ?? "候选修订测试失败");
            streamError = errorMessage;
            setRevisionTestSession((current) => current && current.debug_session_id === session.debug_session_id ? {
              ...current,
              transcript: current.transcript.map((item) => item.message_id === optimisticAssistantId
                ? { ...item, content: item.content || errorMessage }
                : item),
            } : current);
            setNotice({ tone: "error", text: errorMessage });
          }
        },
        { signal: streamAbortController.signal },
      );
      if (streamError) throw new Error(streamError);
      setRevisionTestInput("");
      setCandidateTargetExpertId("");
      setNotice({
        tone: "ok",
        text: `已按 r${selectedTestRevision.revision_no} 的候选快照完成测试。`,
      });
    } catch (error) {
      // Stopping a candidate turn intentionally aborts this browser-side SSE
      // reader. It is not a user-visible request failure.
      if (streamAbortController?.signal.aborted) return;
      setNotice({
        tone: "error",
        text: error instanceof Error ? error.message : "候选修订测试失败",
      });
    } finally {
      const ownsStream = candidateStreamAbortRef.current === streamAbortController;
      if (ownsStream) {
        candidateStreamAbortRef.current = null;
        setBusy(false);
      }
    }
  }

  async function stopCandidateRevisionTest() {
    if (!actor || !revisionTestSession || !candidateConversationState?.run_id || !busy) return;
    try {
      const stopped = await workbenchApi.stopRevisionTestTurn(
        apiBaseUrl,
        revisionTestSession.debug_session_id,
        candidateConversationState.run_id,
        actor.actor_id,
      );
      if (stopped.state?.protocol === "hailiang.sse.v2") {
        setCandidateConversationState(stopped.state as unknown as SseV2State);
      }
      // The server has recorded the cancellation marker. Release the editor
      // immediately instead of waiting for a slow upstream model connection
      // to return. The old reader is intentionally aborted; its finally block
      // is ownership-guarded so it cannot clear a newer turn's loading state.
      const stream = candidateStreamAbortRef.current;
      if (stream) {
        candidateStreamAbortRef.current = null;
        stream.abort();
      }
      setBusy(false);
      setNotice({ tone: "ok", text: "已停止本轮候选测试，可以继续编辑并发送下一条问题。" });
    } catch (error) {
      setNotice({
        tone: "error",
        text: error instanceof Error ? error.message : "停止候选修订测试失败",
      });
    }
  }

  async function submitCandidateFactForm(
    messageId: string,
    formId: string,
    _fields: FactFormField[],
    values: Record<string, unknown>,
  ) {
    await runRevisionTestTurn(undefined, { source_message_id: messageId, form_id: formId, values });
  }

  function confirmCandidateTeamHandoff(sourceMessageId: string, handoff: TeamHandoff, targetExpertId: string) {
    const candidate = handoff.candidates.find((item) => item.expert_id === targetExpertId);
    void runRevisionTestTurn(undefined, undefined, {
      source_message_id: sourceMessageId,
      handoff_id: handoff.handoff_id ?? "",
      target_expert_id: targetExpertId,
      team_id: handoff.team_id,
      mention_name: candidate?.mention_name || candidate?.name || targetExpertId,
    });
  }

  async function completeRevisionTest() {
    if (!actor || !revisionTestSession) return;
    setBusy(true);
    try {
      await workbenchApi.completeDebugSession(
        apiBaseUrl,
        revisionTestSession.debug_session_id,
        {
          conclusion: "业务人员已在候选修订测试台完成验证，并确认当前修订可进入发布流程。",
          actor_id: actor.actor_id,
        },
      );
      setRevisionTestSession({ ...revisionTestSession, status: "completed" });
      setDebugEvidenceId(revisionTestSession.debug_session_id);
      setDebugComplete(true);
      if (debugObject && selectedTestRevision) {
        await loadDebugHistory(debugObject.object_id, selectedTestRevision.revision_id);
      }
      setNotice({ tone: "ok", text: "候选修订测试已记录，可作为发布证据。" });
    } catch (error) {
      setNotice({
        tone: "error",
        text: error instanceof Error ? error.message : "测试证据记录失败",
      });
    } finally {
      setBusy(false);
    }
  }

  async function openFormalTeamChat() {
    if (!actor || !debugObject || !selectedTestRevision || !selectedTestRelease) return;
    setBusy(true);
    try {
      const session = await workbenchApi.createFormalChatSession(apiBaseUrl, {
        object_id: debugObject.object_id,
        revision_id: selectedTestRevision.revision_id,
        release_id: selectedTestRelease.release_id,
        soul_revision_id: soulRevisionId || null,
        actor_id: actor.actor_id,
      });
      window.open(
        `/?workbench_debug_session_id=${encodeURIComponent(session.debug_session_id)}`,
        "_blank",
        "noopener,noreferrer",
      );
      setNotice({ tone: "ok", text: `已打开 ${selectedTestRelease.version} 的固定版本长对话测试台。` });
    } catch (error) {
      setNotice({
        tone: "error",
        text: error instanceof Error ? error.message : "正式专家团对话台打开失败",
      });
    } finally {
      setBusy(false);
    }
  }

  async function publishRevision() {
    if (!actor || !selectedTestRevision) {
      setNotice({ tone: "error", text: "请先选择要发布的修订版本。" });
      return;
    }
    if (!selectedTestRevision.validation.valid) {
      setNotice({ tone: "error", text: "当前修订校验未通过，不能固化发布。" });
      return;
    }
    if (!debugEvidenceId) {
      setNotice({ tone: "error", text: "请先完成手动测试或批量用例，并点击“选为发布证据”。" });
      return;
    }
    if (!manualConfirmed) {
      setNotice({ tone: "error", text: "请勾选“确认业务效果满足发布要求”。" });
      return;
    }
    setBusy(true);
    try {
      const published = await workbenchApi.publish(apiBaseUrl, {
        revision_id: selectedTestRevision.revision_id,
        evidence_id: debugEvidenceId,
        manual_confirmation: manualConfirmed,
        confirmation_notes: "已人工核对对话、路由、工具调用与关键事实。",
        actor_id: actor.actor_id,
      });
      await loadAll();
      await loadDebugTarget(debugObject!.object_id, selectedTestRevision.revision_id);
      setNotice({
        tone: "ok",
        text: `${published.version} 已固化，可被上层对象选择。`,
      });
    } catch (error) {
      setNotice({
        tone: "error",
        text: error instanceof Error ? error.message : "发布失败",
      });
    } finally {
      setBusy(false);
    }
  }

  async function runEvaluationSuite() {
    if (!actor || !debugObject || !selectedTestRevision) return;
    const inputs = evaluationInputs
      .split("\n")
      .map((item) => item.trim())
      .filter(Boolean);
    if (!inputs.length) {
      setNotice({ tone: "error", text: "请至少填写一条测试问题。" });
      return;
    }
    setBusy(true);
    try {
      const suite = await workbenchApi.createEvaluationSuite(apiBaseUrl, {
        object_id: debugObject.object_id,
        name: `${debugObject.name} · r${selectedTestRevision.revision_no} 快速回归`,
        cases: inputs.map((input, index) => ({
          case_id: `case_${index + 1}`,
          name: `用例 ${index + 1}`,
          input,
        })),
        actor_id: actor.actor_id,
      });
      const run = await workbenchApi.createEvaluationRun(apiBaseUrl, {
        suite_id: suite.suite_id,
        revision_id: selectedTestRevision.revision_id,
        baseline_release_id: selectedTestRelease?.release_id ?? null,
        soul_revision_id: soulRevisionId || null,
        results: null,
        actor_id: actor.actor_id,
      });
      setEvaluationRun(run);
      await loadDebugHistory(debugObject.object_id, selectedTestRevision.revision_id);
      setNotice({
        tone: "ok",
        text: `已完成 ${run.results.length} 条用例运行，请人工核对结果。`,
      });
    } catch (error) {
      setNotice({
        tone: "error",
        text: error instanceof Error ? error.message : "用例运行失败",
      });
    } finally {
      setBusy(false);
    }
  }

  async function confirmEvaluationRun() {
    if (!actor || !evaluationRun) return;
    setBusy(true);
    try {
      const completed = await workbenchApi.completeEvaluationRun(
        apiBaseUrl,
        evaluationRun.run_id,
        {
          results: evaluationRun.results,
          manual_result: "accepted",
          manual_notes: "已人工核对批量用例回答、路由、工具调用与关键事实。",
          actor_id: actor.actor_id,
        },
      );
      setEvaluationRun(completed);
      setDebugEvidenceId(completed.run_id);
      setDebugComplete(true);
      setManualConfirmed(true);
      if (debugObject && selectedTestRevision) {
        await loadDebugHistory(debugObject.object_id, selectedTestRevision.revision_id);
      }
      setNotice({ tone: "ok", text: "批量用例已人工确认，可用于发布证据。" });
    } catch (error) {
      setNotice({
        tone: "error",
        text: error instanceof Error ? error.message : "确认失败",
      });
    } finally {
      setBusy(false);
    }
  }

  async function exportRelease(release: ObjectRelease) {
    if (!actor) return;
    setBusy(true);
    try {
      const blob = await workbenchApi.exportRelease(
        apiBaseUrl,
        release.release_id,
        actor.actor_id,
      );
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `${release.object_key}-${release.version}.zip`;
      link.click();
      URL.revokeObjectURL(url);
      setNotice({ tone: "ok", text: "配置包已生成，包含全部精确依赖。" });
    } catch (error) {
      setNotice({
        tone: "error",
        text: error instanceof Error ? error.message : "导出失败",
      });
    } finally {
      setBusy(false);
    }
  }

  async function exportRevision(revision: ObjectRevision) {
    if (!actor || !selectedObject) return;
    setBusy(true);
    try {
      const blob = await workbenchApi.exportRevision(apiBaseUrl, revision.revision_id, actor.actor_id);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `${selectedObject.object_key}-r${revision.revision_no}-candidate.zip`;
      link.click();
      URL.revokeObjectURL(url);
      setNotice({ tone: "ok", text: "候选修订配置包已生成。" });
    } catch (error) {
      setNotice({ tone: "error", text: error instanceof Error ? error.message : "候选修订导出失败" });
    } finally { setBusy(false); }
  }

  async function changeRelease(release: ObjectRelease, draft: boolean) {
    if (!actor) return;
    setBusy(true);
    try {
      if (draft) {
        await workbenchApi.draftFromRelease(apiBaseUrl, release.release_id, actor.actor_id);
      } else {
        const current = releases.find((item) => item.object_id === release.object_id && item.is_current);
        await workbenchApi.makeCurrent(apiBaseUrl, release.release_id, current?.release_id ?? null, actor.actor_id);
      }
      await loadAll();
      await openObject(release.object_id);
      setNotice({tone: "ok", text: draft ? "已创建待调试草稿。" : "已切换当前发布版本，生产部署需单独激活。"});
    } catch (error) {
      setNotice({tone: "error", text: error instanceof Error ? error.message : "版本操作失败"});
    } finally {
      setBusy(false);
    }
  }

  async function importPackage(file: File) {
    if (!actor) return;
    setBusy(true);
    setNotice({ tone: "ok", text: "正在校验配置包并导入生产暂存区…" });
    try {
      await workbenchApi.importPackage(apiBaseUrl, file, actor.actor_id);
      await loadAll();
      setNotice({ tone: "ok", text: "配置包已通过校验并进入生产暂存区。" });
    } catch (error) {
      setNotice({
        tone: "error",
        text: error instanceof Error ? error.message : "导入失败",
      });
    } finally {
      setBusy(false);
    }
  }

  async function importObjectPackage(file: File) {
    if (!actor) return;
    setBusy(true);
    setNotice({ tone: "ok", text: "正在校验配置包并递归导入专家团依赖…" });
    try {
      const result: WorkbenchObjectImportResult =
        await workbenchApi.importObjectPackage(apiBaseUrl, file, actor.actor_id);
      await loadAll();
      setNotice({
        tone: "ok",
        text: `已导入 ${result.created_objects} 个新对象，新增 ${result.created_revisions} 个修订、${result.created_releases} 个发布；复用 ${result.reused_releases} 个已有发布版本${result.unarchived_objects ? `，并恢复 ${result.unarchived_objects} 个此前归档的依赖对象` : ""}。`,
      });
    } catch (error) {
      setNotice({
        tone: "error",
        text: error instanceof Error ? error.message : "工作台对象导入失败",
      });
    } finally {
      setBusy(false);
    }
  }

  async function changeDeployment(
    deployment: Deployment,
    action: "activate" | "rollback" | "restore",
  ) {
    if (!actor) return;
    setBusy(true);
    try {
      if (action === "activate")
        await workbenchApi.activateDeployment(
          apiBaseUrl,
          deployment.deployment_id,
          actor.actor_id,
        );
      else if (action === "rollback")
        await workbenchApi.rollbackDeployment(
          apiBaseUrl,
          deployment.deployment_id,
          actor.actor_id,
        );
      else
        await workbenchApi.restoreDeployment(apiBaseUrl, deployment.deployment_id, actor.actor_id);
      await loadAll();
      setNotice({
        tone: "ok",
        text:
          action === "activate"
            ? "生产版本已激活，新会话将使用该快照。"
            : action === "rollback"
              ? "已回滚到上一部署快照。"
              : "历史专家团版本已恢复，新会话将使用该快照。",
      });
    } catch (error) {
      setNotice({
        tone: "error",
        text: error instanceof Error ? error.message : "部署操作失败",
      });
    } finally {
      setBusy(false);
    }
  }

  async function deactivateDeployment() {
    if (!actor || !deactivateDeploymentTarget || deactivateTeamConfirmation !== deactivateDeploymentTarget.expert_team_id) return;
    setBusy(true);
    try {
      await workbenchApi.deactivateDeployment(
        apiBaseUrl,
        deactivateDeploymentTarget.deployment_id,
        deactivateTeamConfirmation,
        actor.actor_id,
      );
      setDeactivateDeploymentTarget(null);
      setDeactivateTeamConfirmation("");
      await loadAll();
      setNotice({ tone: "ok", text: "正式专家团已下线；新会话将回退到默认通用对话，已有会话保持原快照。" });
    } catch (error) {
      setNotice({ tone: "error", text: error instanceof Error ? error.message : "下线生产专家团失败" });
    } finally {
      setBusy(false);
    }
  }

  if (!actor) {
    return (
      <main className="workbench-shell min-h-screen bg-[#08111f] px-4 py-12 text-white">
        <div className="mx-auto flex min-h-[75vh] max-w-5xl items-center justify-center">
          <section className="grid w-full overflow-hidden rounded-[32px] border border-white/10 bg-slate-950/70 shadow-2xl lg:grid-cols-[1.1fr_0.9fr]">
            <div className="bg-[radial-gradient(circle_at_top_left,_rgba(14,165,233,0.28),_transparent_42%),linear-gradient(145deg,#0f2743,#101827)] p-10 lg:p-14">
              <StatusBadge tone="blue">可信内网 · 一期工作台</StatusBadge>
              <h1 className="mt-6 text-4xl font-semibold tracking-tight">
                让业务配置从“能调”走到“可发布”
              </h1>
              <p className="mt-5 max-w-xl text-base leading-8 text-slate-300">
                Skill、专家与专家团共用一条版本链。每次保存都有据可查，每次上线都能锁定依赖、生成配置包并安全回滚。
              </p>
              <div className="mt-10 grid gap-3 sm:grid-cols-3">
                {["不可变修订", "精确版本组合", "生产快照回滚"].map((item) => (
                  <div
                    key={item}
                    className="rounded-2xl border border-white/10 bg-white/5 px-4 py-4 text-sm text-slate-200"
                  >
                    {item}
                  </div>
                ))}
              </div>
            </div>
            <div className="p-8 lg:p-12">
              <CircleUserRound className="text-sky-300" size={34} />
              <h2 className="mt-6 text-2xl font-semibold">先设置你的用户名</h2>
              <p className="mt-3 text-sm leading-6 text-slate-400">
                无需密码。用户名只用于记录谁保存、发布或激活了配置，不构成安全认证。
              </p>
              <label className="mt-8 block text-sm text-slate-300">
                用户名
                <input
                  autoFocus
                  value={actorName}
                  onChange={(event) => setActorName(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") void registerActor();
                  }}
                  placeholder="例如：王小明"
                  className="mt-2 w-full rounded-2xl border border-slate-700 bg-slate-900 px-4 py-3.5 outline-none transition focus:border-sky-400"
                />
              </label>
              <button
                type="button"
                disabled={busy || actorName.trim().length < 2}
                onClick={() => void registerActor()}
                className="mt-5 w-full rounded-2xl bg-sky-400 px-4 py-3.5 font-semibold text-slate-950 transition hover:bg-sky-300 disabled:opacity-40"
              >
                进入业务调试台
              </button>
            </div>
          </section>
        </div>
      </main>
    );
  }

  return (
    <main className="workbench-shell min-h-screen bg-[#07101d] text-slate-100">
      <div className="flex min-h-screen">
        <aside className="workbench-sidebar hidden w-[248px] shrink-0 border-r border-white/10 bg-[#091422] p-5 lg:flex lg:flex-col">
          <div className="flex items-center gap-3 px-2">
            <div className="grid h-10 w-10 place-items-center rounded-xl bg-sky-400 text-slate-950">
              <Sparkles size={20} />
            </div>
            <div>
              <p className="font-semibold">AI 业务调试台</p>
              <p className="text-xs text-slate-500">Business Workbench</p>
            </div>
          </div>
          <nav className="mt-8 space-y-1">
            {[
              { id: "objects" as const, label: "对象工作区", icon: Boxes },
              { id: "versions" as const, label: "版本中心", icon: History },
              {
                id: "evaluation" as const,
                label: "调试与用例",
                icon: FlaskConical,
              },
              { id: "convert" as const, label: "标准协议转换", icon: Braces },
              { id: "release" as const, label: "发布中心", icon: Rocket },
            ].map((item) => {
              const Icon = item.icon;
              return (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => setSection(item.id)}
                  className={`flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-sm transition ${section === item.id ? "bg-sky-400/15 text-sky-200" : "text-slate-400 hover:bg-white/5 hover:text-white"}`}
                >
                  <Icon size={17} />
                  {item.label}
                </button>
              );
            })}
          </nav>
          <div className="mt-auto space-y-3">
            <a
              href="/"
              className="block rounded-xl border border-sky-400/25 bg-sky-400/[0.06] px-3 py-2.5 text-sm text-sky-100 transition hover:bg-sky-400/[0.12]"
            >
              返回海亮 Skill 调试平台
            </a>
            <p className="rounded-xl border border-white/10 px-3 py-2.5 text-xs leading-5 text-slate-500">
              正式版本长对话验证请从“调试与用例”的已发布专家或专家团入口进入。
            </p>
            <div className="rounded-2xl border border-white/10 bg-white/[0.035] p-3">
              <p className="text-xs text-slate-500">当前操作人</p>
              <p className="mt-1 truncate text-sm font-medium">
                {actor.display_name}
              </p>
            </div>
          </div>
        </aside>

        <div className="min-w-0 flex-1">
          <header className="workbench-header sticky top-0 z-20 border-b border-white/10 bg-[#07101d]/90 px-5 py-4 backdrop-blur-xl lg:px-8">
            <div className="flex flex-wrap items-center justify-between gap-4">
              <div>
                <p className="text-xs uppercase tracking-[0.2em] text-sky-400">
                  {section === "objects" ? "Configuration" : section}
                </p>
                <h1 className="mt-1 text-xl font-semibold">
                  {section === "release"
                    ? "生产发布中心"
                    : section === "evaluation"
                      ? "调试与效果验证"
                    : section === "versions"
                        ? "版本与依赖"
                        : section === "convert"
                          ? "标准协议转换"
                        : "业务对象工作区"}
                </h1>
              </div>
              <div className="flex items-center gap-2">
                <StatusBadge tone="green">
                  <span className="mr-1.5 h-1.5 w-1.5 rounded-full bg-emerald-300" />
                  内核已对齐
                </StatusBadge>
                <div className="flex items-center rounded-xl border border-white/10 bg-slate-950/70 p-1" aria-label="主题模式">
                  {[
                    { value: "dark", label: "夜间", icon: MoonStar },
                    { value: "light", label: "白天", icon: SunMedium },
                  ].map((mode) => {
                    const Icon = mode.icon;
                    return (
                      <button
                        key={mode.value}
                        type="button"
                        onClick={() => setThemeMode(mode.value as "dark" | "light")}
                        className={`inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs transition ${themeMode === mode.value ? "bg-sky-400/20 text-sky-100" : "text-slate-400 hover:text-white"}`}
                        aria-label={`切换${mode.label}模式`}
                      >
                        <Icon size={14} />
                        <span className="hidden sm:inline">{mode.label}</span>
                      </button>
                    );
                  })}
                </div>
                <button
                  type="button"
                  onClick={() => void loadAll()}
                  className="rounded-xl border border-white/10 p-2.5 text-slate-400 hover:text-white"
                  aria-label="刷新"
                >
                  <RefreshCcw size={16} />
                </button>
              </div>
            </div>
          </header>

          {notice ? (
            <div
              className={`mx-5 mt-5 flex items-center justify-between rounded-2xl border px-4 py-3 text-sm lg:mx-8 ${notice.tone === "ok" ? "border-emerald-400/20 bg-emerald-400/10 text-emerald-100" : "border-rose-400/20 bg-rose-400/10 text-rose-100"}`}
            >
              <span>{notice.text}</span>
              <button type="button" onClick={() => setNotice(null)}>
                <X size={16} />
              </button>
            </div>
          ) : null}

          {section === "objects" ? (
            <div className="grid min-h-[calc(100vh-82px)] xl:grid-cols-[390px_minmax(0,1fr)]">
              <section className="border-r border-white/10 p-5 lg:p-7">
                <div className="grid grid-cols-3 gap-3">
                  {(Object.keys(TYPE_META) as WorkbenchObjectType[]).map(
                    (type) => {
                      const Icon = TYPE_META[type].icon;
                      return (
                        <button
                          key={type}
                          type="button"
                          onClick={() =>
                            setTypeFilter(typeFilter === type ? "all" : type)
                          }
                          className={`rounded-2xl border p-3 text-left transition ${typeFilter === type ? "border-sky-400/40 bg-sky-400/10" : "border-white/10 bg-white/[0.03] hover:bg-white/[0.06]"}`}
                        >
                          <Icon size={17} className={TYPE_META[type].tone} />
                          <p className="mt-3 text-xl font-semibold">
                            {
                              objects.filter(
                                (item) => item.object_type === type,
                              ).length
                            }
                          </p>
                          <p className="mt-1 text-xs text-slate-500">
                            {TYPE_META[type].label}
                          </p>
                        </button>
                      );
                    },
                  )}
                </div>
                <div className="mt-5 flex gap-2">
                  <label className="flex min-w-0 flex-1 items-center gap-2 rounded-xl border border-white/10 bg-slate-950/50 px-3">
                    <Search size={15} className="text-slate-500" />
                    <input
                      value={search}
                      onChange={(event) => setSearch(event.target.value)}
                      placeholder="搜索名称或 ID"
                      className="min-w-0 flex-1 bg-transparent py-2.5 text-sm outline-none"
                    />
                  </label>
                  <button
                    type="button"
                    onClick={() => setShowCreate(true)}
                    className="rounded-xl bg-sky-400 px-3 text-slate-950"
                    aria-label="新建对象"
                  >
                    <Plus size={18} />
                  </button>
                </div>
                <label className="mt-3 flex cursor-pointer items-center gap-2 text-xs text-slate-400">
                  <input
                    type="checkbox"
                    checked={showArchived}
                    onChange={(event) => setShowArchived(event.target.checked)}
                    className="h-4 w-4 rounded border-white/15 bg-slate-950 text-sky-400"
                  />
                  显示已归档对象
                </label>
                <div className="mt-4 space-y-2">
                  {filteredObjects.map((item) => {
                    const meta = TYPE_META[item.object_type];
                    const Icon = meta.icon;
                    return (
                      <button
                        key={item.object_id}
                        type="button"
                        onClick={() => void openObject(item.object_id)}
                        className={`w-full rounded-2xl border p-4 text-left transition ${selectedId === item.object_id ? "border-sky-400/35 bg-sky-400/[0.09]" : "border-white/10 bg-white/[0.025] hover:bg-white/[0.055]"}`}
                      >
                        <div className="flex items-start gap-3">
                          <div className="rounded-xl bg-slate-800 p-2">
                            <Icon size={16} className={meta.tone} />
                          </div>
                          <div className="min-w-0 flex-1">
                            <div className="flex items-center justify-between gap-2">
                              <p className="truncate text-sm font-medium">
                                {item.name}
                              </p>
                              <ChevronRight
                                size={15}
                                className="text-slate-600"
                              />
                            </div>
                            <p className="mt-1 truncate font-mono text-[11px] text-slate-500">
                              {item.object_key}
                            </p>
                            {item.object_type !== "skill" && item.brief ? (
                              <p className="mt-2 line-clamp-2 text-xs leading-5 text-slate-400">{item.brief}</p>
                            ) : null}
                            <div className="mt-3 flex gap-2">
                              {item.archived ? <StatusBadge tone="slate">已归档</StatusBadge> : null}
                              <StatusBadge>
                                r{item.latest_revision_no}
                              </StatusBadge>
                              {item.latest_release_no ? (
                                <StatusBadge tone="green">
                                  v{item.latest_release_no}
                                </StatusBadge>
                              ) : (
                                <StatusBadge tone="amber">未发布</StatusBadge>
                              )}
                            </div>
                          </div>
                        </div>
                      </button>
                    );
                  })}
                </div>
              </section>

              <section className="p-5 lg:p-8">
                {!selectedObject ? (
                  <div className="grid min-h-[560px] place-items-center rounded-[28px] border border-dashed border-white/10 bg-white/[0.02] text-center">
                    <div>
                      <Layers3 className="mx-auto text-slate-600" size={42} />
                      <h2 className="mt-5 text-lg font-medium">
                        选择一个对象开始配置
                      </h2>
                      <p className="mt-2 text-sm text-slate-500">
                        每次保存都会生成新的不可变修订。
                      </p>
                    </div>
                  </div>
                ) : (
                  <div className="mx-auto max-w-5xl">
                    <div className="flex flex-wrap items-start justify-between gap-5">
                      <div>
                        <div className="flex items-center gap-2">
                          <StatusBadge tone="blue">
                            {TYPE_META[selectedObject.object_type].label}
                          </StatusBadge>
                          {latestRevision?.validation.valid ? (
                            <StatusBadge tone="green">校验通过</StatusBadge>
                          ) : (
                            <StatusBadge tone="amber">等待校验</StatusBadge>
                          )}
                        </div>
                        <h2 className="mt-4 text-3xl font-semibold tracking-tight">
                          {selectedObject.name}
                        </h2>
                        <p className="mt-2 font-mono text-xs text-slate-500">
                          {selectedObject.object_key} ·{" "}
                          {shortHash(latestRevision?.content_hash)}
                        </p>
                      </div>
                      <div className="flex flex-wrap items-center gap-2">
                        {selectedObject.archived ? (
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => void unarchiveObject(selectedObject)}
                            className="inline-flex items-center gap-2 rounded-xl border border-emerald-400/25 bg-emerald-400/[0.06] px-4 py-2.5 text-sm font-medium text-emerald-100 hover:bg-emerald-400/10 disabled:opacity-40"
                          >
                            <ArchiveRestore size={16} />
                            恢复归档
                          </button>
                        ) : (
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => {
                              setArchiveTarget(selectedObject);
                              setArchiveConfirmation("");
                            }}
                            className="inline-flex items-center gap-2 rounded-xl border border-white/10 px-4 py-2.5 text-sm font-medium text-slate-200 hover:bg-white/5 disabled:opacity-40"
                          >
                            <Archive size={16} />
                            归档
                          </button>
                        )}
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => {
                            setIdMigrationTarget(selectedObject);
                            setIdMigrationConfirmation("");
                            setNextObjectKey("");
                          }}
                          className="inline-flex items-center gap-2 rounded-xl border border-amber-400/25 bg-amber-400/[0.06] px-4 py-2.5 text-sm font-medium text-amber-100 hover:bg-amber-400/10 disabled:opacity-40"
                        >
                          <Wrench size={16} />
                          修改 ID
                        </button>
                        <button
                          type="button"
                          disabled={busy || !selectedObject.releases?.length}
                          onClick={() => {
                            const currentRelease = selectedObject.releases?.[0];
                            setReferenceMigrationTarget(selectedObject);
                            setReferenceMigrationConfirmation("");
                            setReferenceFromReleaseId(currentRelease?.release_id ?? "");
                            setReferenceToReleaseId("");
                          }}
                          className="inline-flex items-center gap-2 rounded-xl border border-white/10 px-4 py-2.5 text-sm font-medium text-slate-200 hover:bg-white/5 disabled:opacity-40"
                        >
                          <GitCompareArrows size={16} />
                          迁移引用
                        </button>
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => {
                            setDeleteTarget(selectedObject);
                            setDeleteConfirmation("");
                          }}
                          className="inline-flex items-center gap-2 rounded-xl border border-rose-400/25 bg-rose-400/[0.06] px-4 py-2.5 text-sm font-medium text-rose-200 hover:bg-rose-400/10 disabled:opacity-40"
                        >
                          <Trash2 size={16} />
                          永久删除
                        </button>
                        <button
                          type="button"
                          disabled={busy || !latestRevision}
                          onClick={() => latestRevision && void exportRevision(latestRevision)}
                          className="inline-flex items-center gap-2 rounded-xl border border-white/10 px-4 py-2.5 text-sm font-medium text-slate-200 hover:bg-white/5 disabled:opacity-40"
                        >
                          <Download size={16} />
                          导出最新修订
                        </button>
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => void saveRevision()}
                          className="inline-flex items-center gap-2 rounded-xl bg-sky-400 px-4 py-2.5 text-sm font-semibold text-slate-950 hover:bg-sky-300 disabled:opacity-40"
                        >
                          <Save size={16} />
                          保存为 r{(latestRevision?.revision_no ?? 0) + 1}
                        </button>
                      </div>
                    </div>
                    {latestRevision?.validation.errors?.length ? (
                      <div className="mt-5 rounded-2xl border border-amber-400/20 bg-amber-400/[0.06] px-4 py-3 text-sm text-amber-100">
                        <p className="font-medium">当前修订未通过发布校验</p>
                        <ul className="mt-2 list-disc space-y-1 pl-5 text-xs text-amber-200/75">
                          {latestRevision.validation.errors.map((error) => (
                            <li key={error}>{error}</li>
                          ))}
                        </ul>
                      </div>
                    ) : null}
                    <label className="mt-5 block text-sm text-slate-300">
                      修订说明（可选）
                      <textarea
                        value={changeSummary}
                        onChange={(event) => setChangeSummary(event.target.value)}
                        maxLength={2000}
                        placeholder="记录本次改了什么、为什么改，方便后续测试与发布追溯"
                        className="mt-2 min-h-20 w-full rounded-xl border border-white/10 bg-slate-950 px-3 py-2 text-sm text-slate-100"
                      />
                    </label>
                    <div className="mt-8 grid gap-6">
                      {selectedObject.object_type !== "skill" ? (
                        <div className="rounded-3xl border border-white/10 bg-white/[0.025] p-5">
                          <label className="block text-sm font-medium text-slate-200">
                            Brief <span className="text-rose-300">*</span>
                            <span className="ml-2 text-xs font-normal text-slate-500">面向使用者的一行摘要，1–120 字；不用于专家团分流。</span>
                            <input
                              value={String(payload.brief ?? "")}
                              onChange={(event) => setPayload({ ...payload, brief: event.target.value })}
                              maxLength={120}
                              placeholder={selectedObject.object_type === "expert" ? "例如：为学生提供学习方法与提分规划支持" : "例如：整合升学、学习与成长咨询的协同专家团"}
                              className="mt-3 w-full rounded-2xl border border-white/10 bg-slate-950/70 px-4 py-3 text-sm text-slate-200 outline-none focus:border-sky-400/40"
                            />
                            <span className="mt-2 block text-right text-xs font-normal text-slate-500">{String(payload.brief ?? "").trim().length}/120</span>
                          </label>
                        </div>
                      ) : null}
                      <div className="rounded-3xl border border-white/10 bg-white/[0.025] p-5">
                        <div className="mb-4 flex items-center gap-2">
                          <Code2 size={17} className="text-sky-300" />
                          <h3 className="font-medium">
                            {selectedObject.object_type === "skill"
                              ? "Prompt 与业务规则"
                              : selectedObject.object_type === "expert"
                                ? "专家角色与决策规则"
                                : "团队协同与兜底规则"}
                          </h3>
                        </div>
                        <textarea
                          value={String(
                            payload[
                              selectedObject.object_type === "skill"
                                ? "prompt_markdown"
                                : "rules_markdown"
                            ] ?? "",
                          )}
                          onChange={(event) =>
                            setPayload({
                              ...payload,
                              [selectedObject.object_type === "skill"
                                ? "prompt_markdown"
                                : "rules_markdown"]: event.target.value,
                            })
                          }
                          rows={14}
                          className="w-full resize-y rounded-2xl border border-white/10 bg-slate-950/70 p-4 font-mono text-sm leading-7 text-slate-200 outline-none focus:border-sky-400/40"
                        />
                      </div>
                      {selectedObject.object_type === "skill" ? (
                        <>
                          <div className="rounded-3xl border border-white/10 bg-white/[0.025] p-5">
                            <div className="mb-4 flex items-center gap-2">
                              <Braces size={17} className="text-violet-300" />
                              <h3 className="font-medium">Runtime Contract</h3>
                              <span className="text-xs text-slate-500">
                                仅声明允许读写的事实字段
                              </span>
                            </div>
                            <textarea
                              value={runtimeContractText}
                              onChange={(event) =>
                                setRuntimeContractText(event.target.value)
                              }
                              rows={10}
                              className="w-full rounded-2xl border border-white/10 bg-slate-950/70 p-4 font-mono text-xs leading-6 text-slate-300 outline-none focus:border-violet-400/40"
                            />
                          </div>
                          <div className="rounded-3xl border border-white/10 bg-white/[0.025] p-5">
                            <div className="mb-4 flex items-center gap-2">
                              <Braces size={17} className="text-sky-300" />
                              <h3 className="font-medium">问卷配置</h3>
                              <span className="text-xs text-slate-500">
                                保存到 assets/questionnaire.json，并写回 SKILL.md 声明
                              </span>
                            </div>
                            <div className="mt-3 flex flex-wrap items-center gap-2">
                              <label className="cursor-pointer rounded-xl border border-white/10 px-3 py-2 text-xs text-slate-300 hover:text-white">
                                <Upload className="mr-1.5 inline" size={14} />导入问卷 JSON
                                <input type="file" accept="application/json,.json" className="hidden" onChange={(event) => { importQuestionnaireConfig(event.target.files?.[0]); event.currentTarget.value = ""; }} />
                              </label>
                              <button type="button" onClick={exportQuestionnaireConfig} className="rounded-xl border border-white/10 px-3 py-2 text-xs text-slate-300 hover:text-white">
                                <Download className="mr-1.5 inline" size={14} />导出问卷 JSON
                              </button>
                            </div>
                            {(() => {
                              const config = questionnaireConfigFromSkill();
                              const questions = Array.isArray(config?.questions) ? config.questions : [];
                              return config ? (
                                <div className="mt-4 rounded-2xl border border-violet-300/15 bg-violet-300/[0.04] p-4">
                                  <div className="flex items-center justify-between gap-3 text-xs">
                                    <span className="font-medium text-violet-100">问卷预览 · {String(config.title || "未命名")}</span>
                                    <span className="text-slate-500">{questions.length} 个问题</span>
                                  </div>
                                  <div className="mt-3 grid gap-2 md:grid-cols-2">
                                    {questions.map((question, index) => {
                                      const item = question && typeof question === "object" ? question as Record<string, unknown> : {};
                                      return <div key={`${String(item.id || "question")}-${index}`} className="rounded-xl border border-white/10 bg-slate-950/40 px-3 py-2 text-xs text-slate-300"><span className="text-slate-500">{index + 1}. </span>{String(item.label || item.id || "未命名问题")}<span className="ml-2 text-violet-200/70">{String(item.input_type || "text")}</span></div>;
                                    })}
                                  </div>
                                </div>
                              ) : null;
                            })()}
                          </div>
                        </>
                      ) : null}
                      {selectedObject.object_type !== "skill" ? (
                        <div className="rounded-3xl border border-white/10 bg-white/[0.025] p-5">
                          <div className="flex items-center justify-between">
                            <div>
                              <h3 className="font-medium">锁定下层发布版本</h3>
                              <p className="mt-1 text-sm text-slate-500">
                                可组合任意历史发布版本；上层不跟随下层自动升级。
                              </p>
                            </div>
                            <StatusBadge
                              tone={
                                uniqueSelectedLocks.length >=
                                (selectedObject.object_type === "expert_team"
                                  ? 2
                                  : 1)
                                  ? "green"
                                  : "amber"
                              }
                            >
                              已选 {uniqueSelectedLocks.length}
                            </StatusBadge>
                          </div>
                          {duplicateDependencyObjectIds.length ? (
                            <div className="mt-5 rounded-2xl border border-amber-400/30 bg-amber-400/[0.08] p-4 text-sm text-amber-100">
                              当前草稿中有同一对象锁定多个历史版本。请展开对应对象并保留一个版本后再保存；系统不会自动替换既有锁定。
                            </div>
                          ) : null}
                          <div className="mt-5 space-y-3">
                            {dependencyReleaseGroups.map((group) => {
                              const selected = selectedLocks.filter((lock) => lock.object_id === group.object_id);
                              const expanded = expandedDependencyObjectIds.includes(group.object_id);
                              const selectedLabel = selected.length === 1
                                ? `已锁定 v${selected[0].release_no}`
                                : selected.length > 1
                                  ? `冲突：已锁定 ${selected.map((lock) => `v${lock.release_no}`).join("、")}`
                                  : "未选择版本";
                              return (
                                <div key={group.object_id} className="rounded-2xl border border-white/10 bg-slate-950/40">
                                  <button
                                    type="button"
                                    onClick={() => setExpandedDependencyObjectIds((current) => current.includes(group.object_id) ? current.filter((id) => id !== group.object_id) : [...current, group.object_id])}
                                    className="flex w-full items-center justify-between gap-4 p-4 text-left"
                                    aria-expanded={expanded}
                                  >
                                    <div>
                                      <p className="text-sm font-medium">{group.name}</p>
                                      <p className="mt-1 font-mono text-[11px] text-slate-500">{TYPE_META[group.object_type].label} · {group.object_key} · {group.releases.length} 个发布版本</p>
                                    </div>
                                    <div className="flex items-center gap-2"><StatusBadge tone={selected.length === 1 ? "blue" : selected.length > 1 ? "amber" : "slate"}>{selectedLabel}</StatusBadge>{expanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}</div>
                                  </button>
                                  {expanded ? (
                                    <div className="border-t border-white/10 p-3">
                                      <div className="space-y-2">
                                        {group.releases.map((release) => (
                                          <label key={release.release_id} className={`flex cursor-pointer items-center justify-between gap-3 rounded-xl border p-3 ${selectedReleaseIds.includes(release.release_id) ? "border-sky-400/35 bg-sky-400/[0.08]" : "border-white/10"}`}>
                                            <span className="flex items-center gap-3"><input type="radio" name={`dependency-${group.object_id}`} checked={selectedReleaseIds.includes(release.release_id)} onChange={() => selectDependencyRelease(group.object_id, release.release_id)} className="accent-sky-400" /><span><strong className="text-sm">{release.version}</strong>{release.is_current ? <span className="ml-2 text-xs text-emerald-200">当前发布</span> : null}<span className="mt-1 block text-[11px] text-slate-500">发布人：{release.published_by_display_name || release.published_by || "未知"}</span><span className="mt-1 block font-mono text-[11px] text-slate-500">{shortHash(release.content_hash)}</span></span></span>
                                          </label>
                                        ))}
                                      </div>
                                      {selected.length ? <button type="button" onClick={() => selectDependencyRelease(group.object_id, null)} className="mt-3 text-xs text-slate-400 hover:text-white">取消选择此对象</button> : null}
                                    </div>
                                  ) : null}
                                </div>
                              );
                            })}
                          </div>
                          {selectedObject.object_type === "expert_team" &&
                          uniqueSelectedLocks.length ? (
                            <div className="mt-5 space-y-5">
                              <label className="block text-sm text-slate-300">
                                主协调专家
                                <select
                                  value={String(
                                    payload.coordinator_expert_id ?? "",
                                  )}
                                  onChange={(event) =>
                                    setPayload({
                                      ...payload,
                                      coordinator_expert_id: event.target.value,
                                    })
                                  }
                                  className="mt-2 w-full rounded-xl border border-white/10 bg-slate-950 px-3 py-3 outline-none"
                                >
                                  <option value="">请选择主协调专家</option>
                                  {uniqueSelectedLocks.map((item) => {
                                    const expertRelease = dependencyReleases.find(
                                      (release) => release.release_id === item.release_id,
                                    );
                                    const expertName = expertRelease?.name || item.object_key;
                                    return (
                                      <option
                                        key={item.object_id}
                                        value={item.object_id}
                                      >
                                        {expertName}（{item.object_key}）· v{item.release_no}
                                      </option>
                                    );
                                  })}
                                </select>
                              </label>
                              <div>
                                <p className="text-sm text-slate-300">成员展示名称与分流职责</p>
                                <p className="mt-1 text-xs text-slate-500">
                                  主协调专家会用展示名称、ID 与职责摘要生成受控移交确认卡；名称在当前专家团内必须唯一。
                                </p>
                                <div className="mt-3 grid gap-3">
                                  {teamMemberSettings.map((member) => (
                                    <div key={member.expert_id} className="grid gap-3 rounded-2xl border border-white/10 bg-slate-950/40 p-4 md:grid-cols-2">
                                      <label className="text-xs text-slate-400">
                                        展示名称 · {member.name}（{member.expert_id}）
                                        <input
                                          value={member.mention_name}
                                          onChange={(event) => updateTeamMember(member.expert_id, { mention_name: event.target.value })}
                                          placeholder={member.name}
                                          className="mt-2 w-full rounded-xl border border-white/10 bg-slate-950 px-3 py-2 text-sm text-slate-200 outline-none focus:border-sky-400/40"
                                        />
                                      </label>
                                      <label className="text-xs text-slate-400">
                                        职责摘要（用于分流）
                                        <input
                                          value={member.routing_brief}
                                          onChange={(event) => updateTeamMember(member.expert_id, { routing_brief: event.target.value })}
                                          placeholder="例如：学习提分、学习方法与学科问题"
                                          className="mt-2 w-full rounded-xl border border-white/10 bg-slate-950 px-3 py-2 text-sm text-slate-200 outline-none focus:border-sky-400/40"
                                        />
                                      </label>
                                    </div>
                                  ))}
                                </div>
                              </div>
                            </div>
                          ) : null}
                        </div>
                      ) : null}
                      {selectedObject.object_type === "skill" ? (
                        <SkillFilesEditor
                          files={pendingAssets}
                          onChange={setPendingAssets}
                          onUpload={addAssets}
                        />
                      ) : null}
                    </div>
                  </div>
                )}
              </section>
            </div>
          ) : null}

          {section === "versions" ? (
            <section className="p-5 lg:p-8">
              <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_360px]">
                <div className="rounded-3xl border border-white/10 bg-white/[0.025] p-6">
                  <div className="flex items-center gap-3">
                    <GitCompareArrows className="text-sky-300" />
                    <div>
                      <h2 className="text-lg font-semibold">
                        不可变版本时间线
                      </h2>
                      <p className="text-sm text-slate-500">
                        修订用于调试，发布版本用于被上层锁定。
                      </p>
                    </div>
                  </div>
                  <div className="mt-6 space-y-3">
                    {objects.map((item) => (
                      <button
                        key={item.object_id}
                        type="button"
                        onClick={() => {
                          void openObject(item.object_id);
                          setSection("versions");
                        }}
                        className="flex w-full items-center justify-between rounded-2xl border border-white/10 bg-slate-950/40 px-4 py-4 text-left"
                      >
                        <div>
                          <p className="font-medium">{item.name}</p>
                          <p className="mt-1 text-xs text-slate-500">
                            {TYPE_META[item.object_type].label} ·{" "}
                            {item.object_key}
                          </p>
                        </div>
                        <div className="flex gap-2">
                          <StatusBadge>r{item.latest_revision_no}</StatusBadge>
                          <StatusBadge
                            tone={item.latest_release_no ? "green" : "amber"}
                          >
                            {item.latest_release_no
                              ? `v${item.latest_release_no}`
                              : "未发布"}
                          </StatusBadge>
                        </div>
                      </button>
                    ))}
                  </div>
                </div>
                <aside className="rounded-3xl border border-white/10 bg-white/[0.025] p-6">
                  <h3 className="font-medium">当前对象版本</h3>
                  {selectedObject?.revisions?.length ? (
                    <div className="mt-5 space-y-3">
                      {selectedObject.revisions.map(
                        (revision: ObjectRevision) => (
                          <div
                            key={revision.revision_id}
                            className="rounded-2xl border border-white/10 p-4"
                          >
                            <div className="flex items-center justify-between">
                              <span className="font-semibold">
                                r{revision.revision_no}
                              </span>
                              {revision.validation.valid ? (
                                <CheckCircle2
                                  size={16}
                                  className="text-emerald-300"
                                />
                              ) : (
                                <Activity
                                  size={16}
                                  className="text-amber-300"
                                />
                              )}
                            </div>
                            <p className="mt-2 font-mono text-[11px] text-slate-500">
                              {shortHash(revision.content_hash)}
                            </p>
                            <p className="mt-2 text-xs text-slate-500">
                              {formatTime(revision.created_at)}
                            </p>
                            <p className="mt-1 text-xs text-slate-500">
                              变更人：{revision.created_by_display_name || revision.created_by || "未知"}
                            </p>
                            {revision.change_summary ? <p className="mt-2 text-xs leading-5 text-slate-400">修订说明：{revision.change_summary}</p> : null}
                            <div className="mt-3 grid grid-cols-2 gap-2">
                              <button
                                type="button"
                                disabled={busy}
                                onClick={() => void exportRevision(revision)}
                                className="inline-flex items-center justify-center gap-1 rounded-lg border border-white/10 px-2 py-1.5 text-xs text-slate-200 hover:bg-white/5 disabled:opacity-40"
                              >
                                <Download size={13} />
                                导出 r{revision.revision_no}
                              </button>
                              <button
                                type="button"
                                onClick={() => {
                                  void loadDebugTarget(selectedObject.object_id, revision.revision_id);
                                  setSection("evaluation");
                                }}
                                className="rounded-lg border border-sky-400/25 px-2 py-1.5 text-xs text-sky-200 hover:bg-sky-400/10"
                              >
                                作为调试目标
                              </button>
                            </div>
                          </div>
                        ),
                      )}
                      <div className="rounded-2xl border border-white/10 p-4">
                        <p className="text-sm font-medium">已发布版本</p>
                        <div className="mt-3 space-y-2">
                          {(selectedObject.releases ?? []).length ? [...(selectedObject.releases ?? [])]
                            .sort((left, right) => Number(right.is_current) - Number(left.is_current) || right.release_no - left.release_no)
                            .map((release) => (
                              <div key={release.release_id} className="flex items-center justify-between gap-3 rounded-xl bg-slate-950/50 px-3 py-2 text-xs">
                                <span><span className="block">{release.version}{release.is_current ? " · 当前发布" : ""}</span><span className="mt-1 block text-[11px] text-slate-500">发布人：{release.published_by_display_name || release.published_by || "未知"} · {formatTime(release.published_at)}</span></span>
                                <span className="font-mono text-slate-500">{shortHash(release.content_hash)}</span>
                              </div>
                            )) : <p className="text-xs text-slate-500">尚未发布版本</p>}
                        </div>
                      </div>
                    </div>
                  ) : (
                    <p className="mt-4 text-sm text-slate-500">
                      先在对象工作区选择一个对象。
                    </p>
                  )}
                </aside>
              </div>
            </section>
          ) : null}

          {section === "evaluation" ? (
            <section className="p-5 lg:p-8 xl:pr-[26vw]">
              <div className="mx-auto max-w-7xl">
                <div className="grid gap-5 md:grid-cols-3">
                  <div className="rounded-3xl border border-white/10 bg-white/[0.025] p-5">
                    <FlaskConical className="text-sky-300" />
                    <p className="mt-5 text-sm text-slate-500">待测试对象</p>
                    <p className="mt-1 text-xl font-semibold">
                      {debugObject?.name ?? "未选择"}
                    </p>
                  </div>
                  <div className="rounded-3xl border border-white/10 bg-white/[0.025] p-5">
                    <GitCompareArrows className="text-violet-300" />
                    <p className="mt-5 text-sm text-slate-500">手动选择的修订</p>
                    <p className="mt-1 text-xl font-semibold">
                      {selectedTestRevision
                        ? `r${selectedTestRevision.revision_no}`
                        : "尚未选择"}
                    </p>
                  </div>
                  <div className="rounded-3xl border border-white/10 bg-white/[0.025] p-5">
                    <ShieldCheck className="text-emerald-300" />
                    <p className="mt-5 text-sm text-slate-500">人工验证</p>
                    <p className="mt-1 text-xl font-semibold">
                      {debugComplete ? "已记录" : "待完成"}
                    </p>
                  </div>
                </div>
                <div className="mt-6 rounded-3xl border border-sky-400/20 bg-sky-400/[0.05] p-5">
                  <label className="block text-sm font-medium text-sky-100">选择调试目标</label>
                  <p className="mt-1 text-xs leading-5 text-slate-400">
                    不会自动使用最新修订。切换修订会清空当前候选测试和用例结果，避免混入其他并行修改。
                  </p>
                  <div className="mt-4 grid gap-3 md:grid-cols-[160px_minmax(0,1fr)]">
                    <select value={debugTypeFilter} onChange={(event) => setDebugTypeFilter(event.target.value as WorkbenchObjectType | "all")} className="rounded-xl border border-white/10 bg-slate-950 px-3 py-3 text-sm outline-none">
                      <option value="all">全部类型</option><option value="skill">Skill</option><option value="expert">专家</option><option value="expert_team">专家团</option>
                    </select>
                    <input value={debugSearch} onChange={(event) => setDebugSearch(event.target.value)} placeholder="按名称或对象 ID 搜索" className="rounded-xl border border-white/10 bg-slate-950 px-3 py-3 text-sm outline-none" />
                  </div>
                  <select
                    value={debugObjectId}
                    onChange={(event) => void loadDebugTarget(event.target.value)}
                    disabled={!filteredDebugObjects.length || busy}
                    className="mt-3 w-full rounded-xl border border-white/10 bg-slate-950 px-3 py-3 text-sm outline-none focus:border-sky-400/50 disabled:opacity-40"
                  >
                    <option value="">请选择对象</option>
                    {filteredDebugObjects.map((item) => <option key={item.object_id} value={item.object_id}>{TYPE_META[item.object_type].label} · {item.name} · {item.object_key}</option>)}
                  </select>
                  <select
                    value={testRevisionId}
                    onChange={(event) => chooseTestRevision(event.target.value)}
                    disabled={!debugObject?.revisions?.length || busy}
                    className="mt-3 w-full rounded-xl border border-white/10 bg-slate-950 px-3 py-3 text-sm outline-none focus:border-sky-400/50 disabled:opacity-40"
                  >
                    <option value="">请选择一个修订版本</option>
                    {(debugObject?.revisions ?? []).map((revision) => (
                      <option key={revision.revision_id} value={revision.revision_id}>
                        r{revision.revision_no} · {revision.created_by_display_name || revision.created_by || "未知"} · {formatTime(revision.created_at)} · {revision.validation.valid ? "校验通过" : "待修复"} · {shortHash(revision.content_hash)}
                      </option>
                    ))}
                  </select>
                  {selectedTestRevision ? (
                    <details className="mt-4 rounded-2xl border border-white/10 bg-slate-950/45 p-4">
                      <summary className="cursor-pointer text-sm font-medium text-slate-200">
                        查看 r{selectedTestRevision.revision_no} 修订记录 · {shortHash(selectedTestRevision.content_hash)}
                      </summary>
                      <div className="mt-4 grid gap-2 text-xs text-slate-400 sm:grid-cols-4">
                        <span>创建：{formatTime(selectedTestRevision.created_at)}</span>
                        <span>变更人：{selectedTestRevision.created_by_display_name || selectedTestRevision.created_by || "未知"}</span>
                        <span>{selectedTestRevision.validation.valid ? "校验通过" : "校验待修复"}</span>
                        <span>{selectedTestRevision.dependency_locks.length} 个锁定依赖</span>
                      </div>
                      <pre className="mt-4 max-h-80 overflow-auto whitespace-pre-wrap rounded-xl border border-white/10 bg-slate-950 p-3 text-xs leading-6 text-slate-300">
                        {String(selectedTestRevision.payload[debugObject?.object_type === "skill" ? "prompt_markdown" : "rules_markdown"] ?? "")}
                      </pre>
                    </details>
                  ) : null}
                  <div className="mt-4 rounded-2xl border border-violet-400/20 bg-violet-400/[0.05] p-4">
                    <p className="text-sm font-medium text-violet-100">Soul 策略（可选）</p>
                    <p className="mt-1 text-xs text-slate-400">候选测试、用例和正式长对话都会使用此处明确选择的 Soul 快照；“不加载”不会读取当前文件。</p>
                    <select value={soulRevisionId} onChange={(event) => setSoulRevisionId(event.target.value)} className="mt-3 w-full rounded-xl border border-white/10 bg-slate-950 px-3 py-2.5 text-sm outline-none">
                      <option value="">不加载 Soul</option>
                      {soulRevisions.map((item) => <option key={item.soul_revision_id} value={item.soul_revision_id}>Soul r{item.revision_no} · {formatTime(item.created_at)} · {shortHash(item.content_hash)}</option>)}
                    </select>
                    {selectedSoulRevision ? <details className="mt-3 text-xs text-slate-400"><summary className="cursor-pointer">查看选中 Soul 内容</summary><pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap rounded-xl bg-slate-950 p-3 leading-6">{selectedSoulRevision.content}</pre></details> : null}
                    <textarea value={soulDraft} onChange={(event) => setSoulDraft(event.target.value)} placeholder="新增 Soul 版本内容（保存后可选择）" rows={3} className="mt-3 w-full rounded-xl border border-white/10 bg-slate-950 p-3 text-xs outline-none" />
                    <button type="button" disabled={!actor || !soulDraft.trim() || busy} onClick={() => void saveSoulRevision()} className="mt-2 rounded-xl border border-violet-400/25 px-3 py-2 text-xs text-violet-200 disabled:opacity-40">保存为新的 Soul 修订</button>
                  </div>
                </div>
                <div className="mt-6 grid gap-6">
                  <div className="order-1 rounded-3xl border border-white/10 bg-white/[0.025] p-6 xl:fixed xl:bottom-0 xl:right-0 xl:top-[82px] xl:z-30 xl:flex xl:w-[25vw] xl:flex-col xl:rounded-none xl:border-y-0 xl:border-r-0 xl:border-l xl:border-white/10 xl:bg-[#091422] xl:p-5 xl:shadow-2xl">
                    <div className="flex items-center justify-between gap-3">
                      <div>
                        <p className="text-xs uppercase tracking-[0.18em] text-sky-300">Debug Zone</p>
                        <h2 className="mt-1 text-lg font-semibold">候选修订手动测试</h2>
                      </div>
                      <div className="flex items-center gap-2">
                        {busy && candidateConversationState?.status === "streaming" ? (
                          <button
                            type="button"
                            onClick={() => void stopCandidateRevisionTest()}
                            className="inline-flex items-center gap-1 rounded-lg border border-rose-300/30 px-2 py-1 text-xs text-rose-100"
                          >
                            <X size={13} /> 停止
                          </button>
                        ) : null}
                        <StatusBadge tone={revisionTestSession?.status === "active" ? "green" : "slate"}>
                          {candidateConversationState?.status === "stopped"
                            ? "已停止"
                            : revisionTestSession?.status === "active" ? "会话进行中" : "等待测试"}
                        </StatusBadge>
                      </div>
                    </div>
                    <p className="mt-2 text-sm text-slate-500">
                      直接测试所选 Skill、专家或专家团的不可变候选快照；不会跳转或写入现有长对话测试台。
                    </p>
                    <div className="mt-3 rounded-xl border border-sky-300/15 bg-sky-300/[0.05] px-3 py-2 text-xs leading-5 text-slate-300">
                      <span className="text-slate-500">候选上下文：</span>未绑定孩子
                      <span className="mx-2 text-slate-600">·</span>
                      <span className="text-slate-500">专家团：</span>{candidateTeamName}
                      <span className="mx-2 text-slate-600">·</span>
                      <span className="text-slate-500">专家：</span>{candidateExpertName}
                      <span className="mx-2 text-slate-600">·</span>
                      <span className="text-slate-500">Skill：</span>{candidateSkillName}
                    </div>
                    {revisionTestSession?.transcript?.length && revisionTestSession.status === "active" ? (
                      <button
                        type="button"
                        onClick={() => void completeRevisionTest()}
                        className="mt-3 w-full rounded-xl border border-emerald-400/25 bg-emerald-400/10 px-4 py-2.5 text-sm text-emerald-200"
                      >
                        记录为候选修订测试证据
                      </button>
                    ) : null}
                    {revisionTestSession?.transcript?.length && selectedTestRevision ? <button type="button" onClick={() => downloadTranscript(
                      selectedTestRevision.revision_id,
                      revisionTestSession.transcript,
                      revisionTestSession.trace,
                      candidateConversationState?.session_id || `revision_test_${revisionTestSession.debug_session_id}`,
                      revisionTestSession.debug_session_id,
                    )} className="mt-3 rounded-xl border border-white/10 px-3 py-2 text-xs text-sky-200">导出纯对话 JSON</button> : null}
                    <div className="mt-5 max-h-[520px] min-h-[360px] space-y-3 overflow-auto rounded-2xl border border-white/10 bg-slate-950/50 p-4 xl:max-h-none xl:min-h-0 xl:flex-1">
                      {revisionTestSession?.transcript?.length ? (
                        revisionTestSession.transcript.map((item, index) => {
                          const isAssistant = item.role === "assistant";
                          const blocks = item.blocks ?? [];
                          const handoffBlock = blocks.find((block) => block.type === "team_handoff");
                          const handoff = item.team_handoff ?? (handoffBlock?.payload as TeamHandoff | undefined);
                          const interactionStates = item.interaction_states ?? {};
                          const presentation = item.presentation as MessagePresentation | undefined;
                          const isTransition = item.message_type === "skill_transition";
                          const isTeamHandoffConfirmation = item.message_type === "team_handoff_confirmation";
                          const transition = presentation?.skill_transition as { action?: string } | undefined;
                          return (
                            <div key={item.message_id || `${item.role}-${index}`} className={item.role === "user" ? "text-right" : "text-left"}>
                              {isTeamHandoffConfirmation ? (
                                <div className="my-1 text-center text-xs text-violet-200/80">
                                  <span className="rounded-full border border-violet-300/25 bg-violet-300/[0.08] px-3 py-1.5">
                                    已确认由 {item.content.replace(/^@/, "")} 专家接管
                                  </span>
                                </div>
                              ) : isAssistant && isTransition ? (
                                <div className="inline-block max-w-[90%] rounded-2xl border border-white/10 bg-white/[0.04] px-4 py-3 text-center text-xs text-slate-300">
                                  {transition?.action === "exit"
                                    ? "已退出当前 Skill"
                                    : `已进入 ${(presentation?.session?.active_skill as { title?: string } | undefined)?.title || "当前 Skill"}`}
                                </div>
                              ) : (
                                <div className={`inline-block max-w-[90%] rounded-2xl px-3 py-2 text-sm leading-6 ${item.role === "user" ? "bg-sky-400 text-slate-950" : "bg-white/10 text-slate-200"}`}>
                                  {isAssistant ? <MarkdownContent content={item.content} className="text-slate-200" /> : item.content}
                                  {isAssistant ? (
                                    <div className="mt-3 text-left">
                                      <MessageBlocksRenderer
                                        messageId={item.message_id || `${revisionTestSession.debug_session_id}-${index}`}
                                        blocks={blocks.filter((block) => block.type !== "team_handoff") as MessageBlock[]}
                                        onPathAction={(pathName, description) => setRevisionTestInput(description || pathName)}
                                        onSubmitFactForm={submitCandidateFactForm}
                                        interactionStates={interactionStates as Record<string, MessageInteractionState>}
                                      />
                                    </div>
                                  ) : null}
                                  {isAssistant && handoff?.candidates?.length ? (
                                    <div className="mt-3 text-left">
                                      <TeamHandoffCard
                                        handoff={handoff}
                                        interactionState={interactionStates.team_handoff as MessageInteractionState | undefined}
                                        disabled={busy || !item.message_id}
                                        onConfirm={(candidate) => confirmCandidateTeamHandoff(item.message_id || "", handoff, candidate.expert_id)}
                                      />
                                    </div>
                                  ) : null}
                                </div>
                              )}
                            </div>
                          );
                        })
                      ) : (
                        <p className="py-8 text-center text-sm text-slate-600">选择一个修订后输入问题，开始候选版本测试。</p>
                      )}
                    </div>
                    {revisionTestSession?.trace?.length ? (
                      <details className="mt-4 rounded-2xl border border-white/10 bg-slate-950/40 p-4">
                        <summary className="cursor-pointer text-sm font-medium text-slate-200">本轮调用轨迹与调试信息</summary>
                        <p className="mt-2 text-xs leading-5 text-slate-500">展开后可查看本轮的专家团、专家、实际 Skill、引用资料和脚本输入/输出；这些内容会一并保存到候选修订测试证据。</p>
                        <CandidateTurnTrace turn={revisionTestSession.trace.at(-1) ?? {}} />
                      </details>
                    ) : null}
                    {candidateTeamMembers.length ? (
                      <div className="mt-4 rounded-2xl border border-violet-300/20 bg-violet-300/[0.05] p-3">
                        <p className="text-xs font-medium text-violet-100">手动 @ 团内专家</p>
                        <div className="mt-2 flex flex-wrap gap-2">
                          {candidateTeamMembers.map((member) => {
                            const selected = member.expert_id === candidateTargetExpertId;
                            return (
                              <button
                                key={member.expert_id}
                                type="button"
                                disabled={busy || activeCandidateHandoff}
                                title={member.routing_brief || `下一条消息交由 ${member.mention_name} 处理`}
                                onClick={() => setCandidateTargetExpertId(selected ? "" : member.expert_id)}
                                className={`rounded-full border px-3 py-1.5 text-xs transition disabled:cursor-not-allowed disabled:opacity-40 ${selected ? "border-violet-200/60 bg-violet-200/15 text-violet-100" : "border-white/10 bg-slate-950/50 text-slate-300 hover:border-violet-400/30 hover:text-violet-100"}`}
                              >
                                {member.is_coordinator ? "主协调：" : "@"}{member.mention_name}
                              </button>
                            );
                          })}
                        </div>
                        <p className="mt-2 text-[11px] leading-5 text-slate-500">
                          {candidateTargetExpert ? `下一条消息将由 @${candidateTargetExpert.mention_name} 接管。` : "选择团内专家后发送问题；选择会绑定本次匿名候选会话。"}
                        </p>
                        {activeCandidateForm ? (
                          <p className="mt-2 text-[11px] leading-5 text-amber-200/80">
                            切换专家并发送新问题会自动结束当前未提交表单；原表单会保留在记录中但不能再提交。
                          </p>
                        ) : null}
                      </div>
                    ) : null}
                    <p className="mt-3 text-[11px] leading-5 text-slate-500">
                      匿名上下文：本会话会持续保留对话、表单状态、Facts 与运行时摘要；不读取、不创建、不写入孩子档案或正式 Profile。
                    </p>
                    <textarea
                      value={revisionTestInput}
                      onChange={(event) => setRevisionTestInput(event.target.value)}
                      placeholder={selectedTestRevision ? `向 r${selectedTestRevision.revision_no} 输入测试问题…` : "请先选择要测试的修订"}
                      rows={3}
                      disabled={!selectedTestRevision || busy || activeCandidateHandoff}
                      className="mt-4 w-full rounded-2xl border border-white/10 bg-slate-950/70 p-4 text-sm leading-7 outline-none focus:border-sky-400/40 disabled:opacity-40"
                    />
                    <button
                      type="button"
                      disabled={!selectedTestRevision || !revisionTestInput.trim() || busy || activeCandidateHandoff}
                      onClick={() => void runRevisionTestTurn(undefined, undefined, undefined, candidateTargetExpert ? { target_expert_id: candidateTargetExpert.expert_id } : undefined)}
                      className="mt-4 inline-flex items-center gap-2 rounded-xl bg-sky-400 px-4 py-2.5 text-sm font-semibold text-slate-950 disabled:opacity-40"
                    >
                      <FlaskConical size={16} />
                      发送测试问题
                    </button>
                  </div>
                  <div className="order-2 rounded-3xl border border-white/10 bg-white/[0.025] p-6">
                    <div className="flex flex-wrap items-center justify-between gap-4">
                      <div>
                        <h2 className="text-lg font-semibold">批量用例回归</h2>
                        <p className="mt-2 text-sm text-slate-500">
                          每行一个问题；每条用例会使用所选修订的独立快照运行并记录回答、路由、Facts 与 Trace。
                        </p>
                      </div>
                    </div>
                    <div className="mt-6 rounded-2xl border border-dashed border-white/10 bg-slate-950/40 p-6">
                      <textarea
                        value={evaluationInputs}
                        onChange={(event) => setEvaluationInputs(event.target.value)}
                        rows={6}
                        disabled={!selectedTestRevision || busy}
                        className="w-full rounded-2xl border border-white/10 bg-slate-950/70 p-4 text-sm leading-7 outline-none focus:border-sky-400/40 disabled:opacity-40"
                      />
                      <button
                        type="button"
                        disabled={!selectedTestRevision || busy}
                        onClick={() => void runEvaluationSuite()}
                        className="mt-5 rounded-xl bg-violet-400 px-4 py-2.5 text-sm font-semibold text-violet-950 disabled:opacity-40"
                      >
                        运行用例集
                      </button>
                    </div>
                    {evaluationRun ? (
                      <div className="mt-5 space-y-3">
                        {evaluationRun.results.map((result, index) => (
                          <div key={String(result.case_id ?? index)} className="rounded-2xl border border-white/10 bg-slate-950/50 p-4">
                            <div className="flex items-center justify-between">
                              <p className="text-sm font-medium">{String(result.name ?? result.case_id ?? `用例 ${index + 1}`)}</p>
                              <StatusBadge tone={result.status === "completed" ? "green" : "amber"}>{String(result.status ?? "pending")}</StatusBadge>
                            </div>
                            <p className="mt-3 line-clamp-3 text-xs leading-6 text-slate-400">{Array.isArray(result.assistant_messages) ? result.assistant_messages.join("\n") : String(result.error ?? "等待运行结果")}</p>
                          </div>
                        ))}
                      </div>
                    ) : null}
                    {evaluationRun && evaluationRun.status === "completed" && evaluationRun.manual_result !== "accepted" ? (
                      <button type="button" onClick={() => void confirmEvaluationRun()} className="mt-4 w-full rounded-xl border border-emerald-400/25 bg-emerald-400/10 px-4 py-2.5 text-sm text-emerald-200">
                        人工确认本次用例通过
                      </button>
                    ) : null}
                  </div>
                </div>
                <div className="mt-6 grid gap-6 xl:grid-cols-2">
                  <div className="rounded-3xl border border-white/10 bg-white/[0.025] p-6">
                    <h2 className="text-lg font-semibold">正式长对话验证</h2>
                    <p className="mt-2 text-sm leading-6 text-slate-500">
                      现有长对话测试台只用于已经发布的专家或专家团固定版本，不承载候选修订测试，也不会影响候选发布证据。
                    </p>
                    <button
                      type="button"
                      disabled={!selectedTestRelease || !["expert", "expert_team"].includes(debugObject?.object_type ?? "") || busy}
                      onClick={() => void openFormalTeamChat()}
                      className="mt-5 rounded-xl border border-white/10 px-4 py-2.5 text-sm text-slate-300 hover:text-white disabled:opacity-40"
                    >
                      打开正式长对话测试台
                    </button>
                    <p className="mt-3 text-xs text-slate-600">
                      {!(["expert", "expert_team"] as string[]).includes(debugObject?.object_type ?? "")
                        ? "请先选择专家或专家团对象。"
                        : selectedTestRelease
                          ? `将验证 ${selectedTestRelease.version} 的固定版本。`
                          : "请先选择已发布的专家或专家团修订。"}
                    </p>
                  </div>
                  <div className="rounded-3xl border border-white/10 bg-white/[0.025] p-6">
                    <h2 className="text-lg font-semibold">候选修订发布确认</h2>
                    {selectedTestRevision?.validation.errors?.length ? (
                      <div className="mt-4 rounded-2xl border border-rose-400/25 bg-rose-400/[0.07] p-4 text-sm text-rose-100">
                        <p className="font-medium">当前修订无法发布，原因如下：</p>
                        <ul className="mt-2 list-disc space-y-1 pl-5 text-xs leading-5 text-rose-200/90">
                          {selectedTestRevision.validation.errors.map((error) => <li key={error}>{error}</li>)}
                        </ul>
                        <p className="mt-3 text-xs text-rose-200/70">请在对象工作区修复后保存为新的修订，再重新测试并记录证据。</p>
                      </div>
                    ) : null}
                    {!selectedTestRevision?.validation.errors?.length && selectedTestRevision?.validation.warnings?.length ? (
                      <div className="mt-4 rounded-2xl border border-amber-400/25 bg-amber-400/[0.06] p-4 text-xs text-amber-100">
                        <p className="font-medium">发布前提示</p>
                        <ul className="mt-2 list-disc space-y-1 pl-5">{selectedTestRevision.validation.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>
                      </div>
                    ) : null}
                    {debugComplete ? (
                      <label className="mt-5 flex items-start gap-3 rounded-2xl border border-emerald-400/20 bg-emerald-400/[0.07] p-4 text-sm">
                        <input
                          type="checkbox"
                          checked={manualConfirmed}
                          onChange={(event) => setManualConfirmed(event.target.checked)}
                          className="mt-1 accent-emerald-400"
                        />
                        <span>
                          <strong className="block text-emerald-100">确认业务效果满足发布要求</strong>
                          <span className="mt-1 block text-emerald-200/70">已核对候选修订的回答、路由、工具调用和关键事实。</span>
                        </span>
                      </label>
                    ) : (
                      <p className="mt-5 text-sm text-slate-500">请先在手动测试或批量用例中完成并人工确认所选修订。</p>
                    )}
                    <button
                      type="button"
                      disabled={!selectedTestRevision || busy}
                      onClick={() => void publishRevision()}
                      className="mt-5 inline-flex items-center gap-2 rounded-xl bg-emerald-400 px-5 py-3 text-sm font-semibold text-emerald-950 disabled:opacity-40"
                    >
                      <PackageCheck size={17} />
                      固化为下一个发布版本
                    </button>
                  </div>
                </div>
                <section className="mt-6 rounded-3xl border border-white/10 bg-white/[0.025] p-6">
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <div><h2 className="text-lg font-semibold">调试历史</h2><p className="mt-1 text-sm text-slate-500">仅展示当前调试对象和修订的记录；已完成证据可直接选作发布依据。</p></div>
                    <div className="flex gap-2"><select value={historyStatus} onChange={(event) => { const status = event.target.value as "all" | "active" | "completed"; setHistoryStatus(status); if (debugObject && selectedTestRevision) void loadDebugHistory(debugObject.object_id, selectedTestRevision.revision_id, status); }} className="rounded-xl border border-white/10 bg-slate-950 px-3 py-2 text-xs"><option value="all">全部状态</option><option value="active">进行中</option><option value="completed">已完成</option></select><button type="button" disabled={!debugObject || !selectedTestRevision || busy} onClick={() => debugObject && selectedTestRevision && void loadDebugHistory(debugObject.object_id, selectedTestRevision.revision_id)} className="rounded-xl border border-white/10 px-3 py-2 text-xs text-slate-300 disabled:opacity-40">刷新历史</button></div>
                  </div>
                  <div className="mt-5 grid gap-4 lg:grid-cols-2">
                    <div className="space-y-3"><p className="text-xs font-medium text-sky-200">手动调试会话</p>{debugHistory.length ? debugHistory.map((item) => <div key={item.debug_session_id} className="rounded-2xl border border-white/10 bg-slate-950/45 p-4"><div className="flex items-center justify-between gap-2"><span>r{item.revision_no} · {item.status === "completed" ? "已完成" : "进行中"}</span><span className="text-xs text-slate-500">{formatTime(item.created_at)}</span></div><p className="mt-2 text-xs text-slate-400">{item.conclusion || "尚未填写结论"}</p><details className="mt-2 text-xs text-slate-400"><summary className="cursor-pointer">查看完整证据</summary><pre className="mt-2 max-h-52 overflow-auto whitespace-pre-wrap rounded-lg bg-slate-950 p-2">{JSON.stringify(item, null, 2)}</pre></details><div className="mt-3 flex gap-3">{item.status === "completed" ? <button type="button" onClick={() => chooseHistoryEvidence(item.debug_session_id)} className="text-xs text-emerald-300">选为发布证据</button> : null}<button type="button" onClick={() => downloadEvidence("debug", item)} className="text-xs text-sky-300">下载 JSON</button></div></div>) : <p className="text-sm text-slate-600">暂无手动调试记录</p>}</div>
                    <div className="space-y-3"><p className="text-xs font-medium text-violet-200">批量评测运行</p>{evaluationHistory.length ? evaluationHistory.map((item) => <div key={item.run_id} className="rounded-2xl border border-white/10 bg-slate-950/45 p-4"><div className="flex items-center justify-between gap-2"><span>{item.suite_name} · r{item.revision_no}</span><span className="text-xs text-slate-500">{formatTime(item.created_at)}</span></div><p className="mt-2 text-xs text-slate-400">{item.manual_result === "accepted" ? "人工通过" : item.manual_result === "rejected" ? "人工拒绝" : item.status}</p><details className="mt-2 text-xs text-slate-400"><summary className="cursor-pointer">查看完整证据</summary><pre className="mt-2 max-h-52 overflow-auto whitespace-pre-wrap rounded-lg bg-slate-950 p-2">{JSON.stringify(item, null, 2)}</pre></details><div className="mt-3 flex gap-3">{item.manual_result === "accepted" ? <button type="button" onClick={() => chooseHistoryEvidence(item.run_id)} className="text-xs text-emerald-300">选为发布证据</button> : null}<button type="button" onClick={() => downloadEvidence("evaluation", item)} className="text-xs text-sky-300">下载 JSON</button></div></div>) : <p className="text-sm text-slate-600">暂无批量评测记录</p>}</div>
                  </div>
                </section>
              </div>
            </section>
          ) : null}

          {section === "convert" ? (
            <section className="p-5 lg:p-8">
              <div className="mx-auto grid max-w-6xl gap-6 xl:grid-cols-[0.8fr_1.2fr]">
                <div className="rounded-3xl border border-white/10 bg-white/[0.025] p-6">
                  <Braces className="text-violet-300" />
                  <h2 className="mt-4 text-xl font-semibold">导入标准 Agent Skills 包</h2>
                  <p className="mt-2 text-sm leading-6 text-slate-400">上传 ZIP 后只会解析 SKILL.md、引用资料和脚本；不会执行脚本，不会自动发布。</p>
                  <label className="mt-6 flex cursor-pointer flex-col items-center rounded-2xl border border-dashed border-violet-400/30 bg-violet-400/[0.05] p-8 text-center text-sm text-violet-100">
                    <CloudUpload size={28} />
                    <span className="mt-3">选择标准 Skill ZIP</span>
                    <input type="file" accept=".zip,application/zip" className="hidden" onChange={(event) => { const file = event.target.files?.[0]; if (file) void previewStandardSkill(file); event.currentTarget.value = ""; }} />
                  </label>
                  <p className="mt-4 text-xs leading-5 text-slate-500">支持 SKILL.md、AGENT.md 或 TEAM.md；Python 脚本与表单定义会经过静态校验。</p>
                </div>
                <div className="rounded-3xl border border-white/10 bg-white/[0.025] p-6">
                  {conversion && conversionDraft ? <>
                    <div className="flex items-start justify-between gap-4"><div><h2 className="text-xl font-semibold">转换草稿</h2><p className="mt-1 text-sm text-slate-400">{conversion.ai_assistance.message}</p></div><StatusBadge tone={conversion.warnings.length ? "amber" : "green"}>{conversion.warnings.length ? `${conversion.warnings.length} 项待处理` : "校验通过"}</StatusBadge></div>
                    {conversion.warnings.length ? <ul className="mt-4 list-disc space-y-1 pl-5 text-xs text-amber-200">{conversion.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul> : null}
                    <div className="mt-5 grid gap-3 md:grid-cols-2">
                      <label className="text-sm text-slate-300">对象类型<select value={String(conversionDraft.object_type ?? "skill")} onChange={(event) => setConversionDraft({ ...conversionDraft, object_type: event.target.value })} className="mt-2 w-full rounded-xl border border-white/10 bg-slate-950 px-3 py-2.5"><option value="skill">Skill</option><option value="expert">专家</option><option value="expert_team">专家团</option></select></label>
                      <label className="text-sm text-slate-300">对象 ID<input value={String(conversionDraft.object_key ?? "")} onChange={(event) => setConversionDraft({ ...conversionDraft, object_key: event.target.value })} className="mt-2 w-full rounded-xl border border-white/10 bg-slate-950 px-3 py-2.5" /></label>
                      <label className="text-sm text-slate-300 md:col-span-2">名称<input value={String(conversionDraft.name ?? "")} onChange={(event) => setConversionDraft({ ...conversionDraft, name: event.target.value })} className="mt-2 w-full rounded-xl border border-white/10 bg-slate-950 px-3 py-2.5" /></label>
                    </div>
                    <details className="mt-5 rounded-2xl border border-white/10 p-4"><summary className="cursor-pointer text-sm">源文件与表单预览</summary><p className="mt-3 text-xs text-slate-400">{conversion.source.files.join(" · ")}</p><pre className="mt-3 max-h-52 overflow-auto rounded-xl bg-slate-950 p-3 text-xs">{JSON.stringify(conversion.form_preview, null, 2)}</pre></details>
                    <label className="mt-5 block text-sm text-slate-300">写入位置<select value={conversionTargetId} onChange={(event) => setConversionTargetId(event.target.value)} className="mt-2 w-full rounded-xl border border-white/10 bg-slate-950 px-3 py-2.5"><option value="">新建对象并保存 r1</option>{objects.filter((item) => item.object_type === conversionDraft.object_type).map((item) => <option key={item.object_id} value={item.object_id}>写入 {item.name} 的新修订</option>)}</select></label>
                    <button type="button" disabled={busy} onClick={() => void commitStandardSkill()} className="mt-5 rounded-xl bg-violet-400 px-5 py-3 text-sm font-semibold text-violet-950 disabled:opacity-40">人工确认并保存候选修订</button>
                  </> : <div className="grid min-h-72 place-items-center text-center text-sm text-slate-500">上传一个标准包后，在这里查看可编辑转换草稿、表单映射和安全校验。</div>}
                </div>
              </div>
            </section>
          ) : null}

          {section === "release" ? (
            <section className="p-5 lg:p-8">
              <div className="grid gap-6 xl:grid-cols-[minmax(0,1.1fr)_minmax(360px,0.9fr)]">
                <div className="rounded-3xl border border-white/10 bg-white/[0.025] p-6">
                  <div className="flex items-center gap-3">
                    <PackageCheck className="text-sky-300" />
                    <div>
                      <h2 className="text-lg font-semibold">已发布配置</h2>
                      <p className="text-sm text-slate-500">
                        专家和专家团导出时自动递归携带全部依赖。
                      </p>
                    </div>
                  </div>
                  <div className="mt-6 space-y-3">
                    {publishedReleaseGroups.map((group) => {
                      const expanded = expandedPublishedObjectIds.includes(group.object_id);
                      const current = group.releases.find((release) => release.is_current);
                      return (
                        <div key={group.object_id} className="rounded-2xl border border-white/10 bg-slate-950/40">
                          <button type="button" onClick={() => setExpandedPublishedObjectIds((items) => items.includes(group.object_id) ? items.filter((id) => id !== group.object_id) : [...items, group.object_id])} className="flex w-full items-center justify-between gap-4 p-4 text-left" aria-expanded={expanded}>
                            <div><p className="font-medium">{group.name}</p><p className="mt-1 text-xs text-slate-500">{TYPE_META[group.object_type].label} · {group.object_key} · {group.releases.length} 个发布版本</p></div>
                            <div className="flex items-center gap-2"><StatusBadge tone={current ? "green" : "slate"}>{current ? `${current.version} · 当前发布` : "无当前发布"}</StatusBadge>{expanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}</div>
                          </button>
                          {expanded ? <div className="space-y-3 border-t border-white/10 p-4">{group.releases.map((release) => (
                            <div key={release.release_id} className="flex flex-wrap items-center justify-between gap-4 rounded-xl border border-white/10 p-3">
                              <div><p className="text-sm font-medium">{release.version}{release.is_current ? <span className="ml-2 text-xs text-emerald-200">当前发布</span> : null}</p><p className="mt-1 text-xs text-slate-500">固化自 r{release.revision_no ?? "?"} · {release.revision_id}</p>{release.revision_change_summary ? <p className="mt-1 text-xs text-slate-400">修订说明：{release.revision_change_summary}</p> : null}<p className="mt-1 text-xs text-slate-500">发布人：{release.published_by_display_name || release.published_by || "未知"} · {formatTime(release.published_at)}</p><p className="mt-1 text-xs text-slate-500">{release.dependency_locks.length} 个锁定依赖 · {shortHash(release.content_hash)}</p></div>
                              <div className="flex flex-wrap gap-2"><button type="button" onClick={() => void exportRelease(release)} className="inline-flex items-center gap-2 rounded-xl border border-white/10 px-3 py-2 text-sm text-slate-300 hover:text-white"><Download size={15} />导出</button><button type="button" disabled={busy || release.is_current} onClick={() => void changeRelease(release, false)} className="rounded-xl border border-amber-400/25 px-3 py-2 text-sm text-amber-100 hover:bg-amber-400/10 disabled:cursor-not-allowed disabled:opacity-40">设为当前发布</button><button type="button" disabled={busy} onClick={() => void changeRelease(release, true)} className="rounded-xl border border-sky-400/25 px-3 py-2 text-sm text-sky-100 hover:bg-sky-400/10 disabled:cursor-not-allowed disabled:opacity-40">从此版本创建草稿</button></div>
                            </div>
                          ))}</div> : null}
                        </div>
                      );
                    })}
                  </div>
                </div>
                <div className="space-y-6">
                  <div className="rounded-3xl border border-white/10 bg-white/[0.025] p-6">
                    <div className="flex items-center gap-3">
                      <Boxes className="text-cyan-300" />
                      <div>
                        <h2 className="font-semibold">递归导入到工作台</h2>
                        <p className="text-sm text-slate-500">
                          将专家团包中的专家与 Skill 一并回灌为可编辑对象，不影响线上部署。
                        </p>
                      </div>
                    </div>
                    <label className="mt-5 flex cursor-pointer flex-col items-center rounded-2xl border border-dashed border-cyan-400/25 bg-cyan-400/[0.05] px-5 py-7 text-center">
                      <Boxes size={25} className="text-cyan-300" />
                      <span className="mt-3 text-sm font-medium">
                        选择配置包 ZIP
                      </span>
                      <span className="mt-1 text-xs text-slate-500">
                        自动递归导入专家团、专家与 Skill 闭包
                      </span>
                      <input
                        type="file"
                        accept=".zip,application/zip"
                        className="hidden"
                        onChange={(event) => {
                          const file = event.target.files?.[0];
                          if (file) void importObjectPackage(file);
                          event.currentTarget.value = "";
                        }}
                      />
                    </label>
                  </div>
                  <div className="rounded-3xl border border-white/10 bg-white/[0.025] p-6">
                    <div className="flex items-center gap-3">
                      <CloudUpload className="text-violet-300" />
                      <div>
                        <h2 className="font-semibold">生产暂存导入</h2>
                        <p className="text-sm text-slate-500">
                          导入不会立即影响线上。
                        </p>
                      </div>
                    </div>
                    <label className="mt-5 flex cursor-pointer flex-col items-center rounded-2xl border border-dashed border-violet-400/25 bg-violet-400/[0.05] px-5 py-7 text-center">
                      <CloudUpload size={25} className="text-violet-300" />
                      <span className="mt-3 text-sm font-medium">
                        选择配置包 ZIP
                      </span>
                      <span className="mt-1 text-xs text-slate-500">
                        自动校验 Schema、哈希、依赖与内核
                      </span>
                      <input
                        type="file"
                        accept=".zip,application/zip"
                        className="hidden"
                        onChange={(event) => {
                          const file = event.target.files?.[0];
                          if (file) void importPackage(file);
                          event.currentTarget.value = "";
                        }}
                      />
                    </label>
                  </div>
                  <div className="rounded-3xl border border-white/10 bg-white/[0.025] p-6">
                    <h2 className="font-semibold">生产部署</h2>
                    <div className="mt-5 space-y-3">
                      {deployments.length ? (
                        deployments.map((deployment) => (
                          <div
                            key={deployment.deployment_id}
                            className="rounded-2xl border border-white/10 bg-slate-950/40 p-4"
                          >
                            <div className="flex items-center justify-between gap-3">
                              <div>
                                <p className="text-sm font-medium">
                                  {deployment.expert_team_name ?? deployment.manifest.root?.name ?? deployment.manifest.root?.object_key ??
                                    deployment.root_release_id}
                                </p>
                                <p className="mt-1 text-xs text-slate-500">
                                  {deployment.expert_team_id ? `expert_team_id: ${deployment.expert_team_id} · ` : ""}{formatTime(deployment.imported_at)}
                                </p>
                              </div>
                              <StatusBadge
                                tone={
                                  deployment.status === "active"
                                    ? "green"
                                    : deployment.status === "staged"
                                      ? "amber"
                                      : "slate"
                                }
                              >
                                {deployment.status}
                              </StatusBadge>
                            </div>
                            <div className="mt-4 flex gap-2">
                              {deployment.status === "staged" ? (
                                <button
                                  type="button"
                                  onClick={() =>
                                    void changeDeployment(
                                      deployment,
                                      "activate",
                                    )
                                  }
                                  className="flex-1 rounded-xl bg-emerald-400 px-3 py-2 text-sm font-semibold text-emerald-950"
                                >
                                  激活
                                </button>
                              ) : null}
                              {deployment.status === "active" &&
                              deployment.previous_deployment_id ? (
                                <button
                                  type="button"
                                  onClick={() =>
                                    void changeDeployment(
                                      deployment,
                                      "rollback",
                                    )
                                  }
                                  className="flex-1 rounded-xl border border-amber-400/25 px-3 py-2 text-sm text-amber-200"
                                >
                                  回滚
                                </button>
                              ) : null}
                              {deployment.status === "active" && deployment.expert_team_id ? (
                                <button
                                  type="button"
                                  onClick={() => {
                                    setDeactivateDeploymentTarget(deployment);
                                    setDeactivateTeamConfirmation("");
                                  }}
                                  className="flex-1 rounded-xl border border-rose-400/25 px-3 py-2 text-sm text-rose-200"
                                >
                                  下线专家团
                                </button>
                              ) : null}
                              {["superseded", "rolled_back", "deactivated"].includes(deployment.status) && deployment.expert_team_id ? (
                                <button
                                  type="button"
                                  onClick={() => void changeDeployment(deployment, "restore")}
                                  className="flex-1 rounded-xl border border-sky-400/25 px-3 py-2 text-sm text-sky-200"
                                >
                                  恢复此版本
                                </button>
                              ) : null}
                            </div>
                          </div>
                        ))
                      ) : (
                        <p className="text-sm text-slate-500">
                          暂无生产暂存或激活记录。
                        </p>
                      )}
                    </div>
                  </div>
                </div>
              </div>
              <div className="mt-6 rounded-3xl border border-white/10 bg-white/[0.025] p-6">
                <div className="flex items-center gap-3">
                  <Activity className="text-amber-300" />
                  <h2 className="font-semibold">最近操作</h2>
                </div>
                <div className="mt-5 grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                  {audit.slice(0, 9).map((event) => (
                    <div
                      key={event.event_id}
                      className="rounded-2xl border border-white/10 bg-slate-950/35 p-3"
                    >
                      <p className="text-sm">{event.action}</p>
                      <p className="mt-2 text-xs text-slate-500">
                        {event.actor_id} · {formatTime(event.created_at)}
                      </p>
                    </div>
                  ))}
                </div>
              </div>
            </section>
          ) : null}
        </div>
      </div>

      {deactivateDeploymentTarget ? (
        <div className="fixed inset-0 z-[60] grid place-items-center bg-black/80 p-4 backdrop-blur-sm">
          <div role="alertdialog" aria-modal="true" className="w-full max-w-lg rounded-3xl border border-rose-400/20 bg-[#151923] p-6 shadow-2xl shadow-black/50">
            <div className="flex items-start justify-between gap-5"><div><p className="text-xs font-medium uppercase tracking-[0.18em] text-rose-300">生产变更确认</p><h2 className="mt-1 text-xl font-semibold">下线正式专家团？</h2></div><button type="button" aria-label="关闭下线确认" onClick={() => setDeactivateDeploymentTarget(null)} className="rounded-xl p-2 text-slate-500 hover:bg-white/5 hover:text-white"><X size={18} /></button></div>
            <div className="mt-5 rounded-2xl border border-rose-400/15 bg-rose-400/[0.05] p-4 text-sm leading-6 text-rose-100/80"><p>专家团：<strong className="text-white">{deactivateDeploymentTarget.expert_team_name ?? deactivateDeploymentTarget.manifest.root?.name ?? deactivateDeploymentTarget.expert_team_id}</strong></p><p className="mt-2">下线后，新正式会话回退默认通用对话；已有会话继续使用当前 ZIP 快照。可从部署历史恢复此版本。</p></div>
            <label className="mt-5 block text-sm text-slate-300">请输入 <strong className="font-mono text-white">{deactivateDeploymentTarget.expert_team_id}</strong> 以确认
              <input autoFocus value={deactivateTeamConfirmation} onChange={(event) => setDeactivateTeamConfirmation(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && deactivateTeamConfirmation === deactivateDeploymentTarget.expert_team_id && !busy) void deactivateDeployment(); }} className="mt-2 w-full rounded-xl border border-white/10 bg-slate-950 px-3 py-3 font-mono outline-none focus:border-rose-400/50" />
            </label>
            <div className="mt-6 grid grid-cols-2 gap-3"><button type="button" disabled={busy} onClick={() => setDeactivateDeploymentTarget(null)} className="rounded-xl border border-white/10 px-4 py-3 text-sm text-slate-300 hover:bg-white/5">取消</button><button type="button" disabled={busy || deactivateTeamConfirmation !== deactivateDeploymentTarget.expert_team_id} onClick={() => void deactivateDeployment()} className="rounded-xl bg-rose-500 px-4 py-3 text-sm font-semibold text-white disabled:opacity-35">确认下线</button></div>
          </div>
        </div>
      ) : null}
      {idMigrationTarget ? (
        <div className="fixed inset-0 z-[60] grid place-items-center bg-black/80 p-4 backdrop-blur-sm">
          <div role="dialog" aria-modal="true" className="w-full max-w-lg rounded-3xl border border-amber-400/20 bg-[#151923] p-6 shadow-2xl shadow-black/50">
            <div className="flex items-start justify-between gap-5">
              <div>
                <p className="text-xs font-medium uppercase tracking-[0.18em] text-amber-200">需确认的 ID 迁移</p>
                <h2 className="mt-1 text-xl font-semibold">为“{idMigrationTarget.name}”创建新 ID</h2>
              </div>
              <button type="button" aria-label="关闭 ID 修改" onClick={() => setIdMigrationTarget(null)} className="rounded-xl p-2 text-slate-500 hover:bg-white/5 hover:text-white"><X size={18} /></button>
            </div>
            <div className="mt-5 rounded-2xl border border-amber-400/15 bg-amber-400/[0.05] p-4 text-sm leading-6 text-amber-50/80">
              <p>当前 {TYPE_META[idMigrationTarget.object_type].label} ID：<strong className="font-mono text-white">{idMigrationTarget.object_key}</strong></p>
              <p className="mt-2 text-xs text-amber-100/65">将复制最新修订为新 ID 的待调试对象；旧对象、已发布版本和生产快照不会改变。</p>
            </div>
            <label className="mt-5 block text-sm text-slate-300">新的 ID
              <input autoFocus value={nextObjectKey} onChange={(event) => setNextObjectKey(event.target.value)} placeholder="仅限字母、数字、_、-" className="mt-2 w-full rounded-xl border border-white/10 bg-slate-950 px-3 py-3 font-mono outline-none focus:border-amber-400/50" />
            </label>
            <label className="mt-4 block text-sm text-slate-300">请输入对象名称 <strong className="text-white">{idMigrationTarget.name}</strong> 以确认
              <input value={idMigrationConfirmation} onChange={(event) => setIdMigrationConfirmation(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && idMigrationConfirmation === idMigrationTarget.name && nextObjectKey.trim() && !busy) void migrateObjectId(); }} className="mt-2 w-full rounded-xl border border-white/10 bg-slate-950 px-3 py-3 outline-none focus:border-amber-400/50" />
            </label>
            <div className="mt-6 grid grid-cols-2 gap-3">
              <button type="button" disabled={busy} onClick={() => setIdMigrationTarget(null)} className="rounded-xl border border-white/10 px-4 py-3 text-sm text-slate-300 hover:bg-white/5">取消</button>
              <button type="button" disabled={busy || idMigrationConfirmation !== idMigrationTarget.name || !nextObjectKey.trim()} onClick={() => void migrateObjectId()} className="rounded-xl bg-amber-400 px-4 py-3 text-sm font-semibold text-slate-950 disabled:opacity-35">确认创建后继草稿</button>
            </div>
          </div>
        </div>
      ) : null}
      {referenceMigrationTarget ? (
        <div className="fixed inset-0 z-[60] grid place-items-center bg-black/80 p-4 backdrop-blur-sm">
          <div role="dialog" aria-modal="true" className="w-full max-w-lg rounded-3xl border border-sky-400/20 bg-[#151923] p-6 shadow-2xl shadow-black/50">
            <div className="flex items-start justify-between gap-5"><div><p className="text-xs font-medium uppercase tracking-[0.18em] text-sky-300">分阶段引用迁移</p><h2 className="mt-1 text-xl font-semibold">生成上游对象的待调试修订</h2></div><button type="button" aria-label="关闭引用迁移" onClick={() => setReferenceMigrationTarget(null)} className="rounded-xl p-2 text-slate-500 hover:bg-white/5 hover:text-white"><X size={18} /></button></div>
            <p className="mt-4 text-sm leading-6 text-slate-300">选定旧发布版本和其已发布替代版本。系统仅生成直接引用它们的最新草稿，不会自动发布或影响生产。</p>
            <label className="mt-4 block text-sm text-slate-300">旧发布版本
              <select value={referenceFromReleaseId} onChange={(event) => setReferenceFromReleaseId(event.target.value)} className="mt-2 w-full rounded-xl border border-white/10 bg-slate-950 px-3 py-3 outline-none">
                {(referenceMigrationTarget.releases ?? []).map((release) => <option key={release.release_id} value={release.release_id}>{release.version} · {release.object_key}</option>)}
              </select>
            </label>
            <label className="mt-4 block text-sm text-slate-300">替代发布版本
              <select value={referenceToReleaseId} onChange={(event) => setReferenceToReleaseId(event.target.value)} className="mt-2 w-full rounded-xl border border-white/10 bg-slate-950 px-3 py-3 outline-none"><option value="">请选择已发布替代版本</option>{releases.filter((release) => release.object_type === referenceMigrationTarget.object_type && release.object_id !== referenceMigrationTarget.object_id && !release.archived).map((release) => <option key={release.release_id} value={release.release_id}>{release.name} · {release.object_key} · {release.version}</option>)}</select>
            </label>
            <label className="mt-4 block text-sm text-slate-300">请输入对象名称 <strong className="text-white">{referenceMigrationTarget.name}</strong> 以确认
              <input value={referenceMigrationConfirmation} onChange={(event) => setReferenceMigrationConfirmation(event.target.value)} className="mt-2 w-full rounded-xl border border-white/10 bg-slate-950 px-3 py-3 outline-none focus:border-sky-400/50" />
            </label>
            <div className="mt-6 grid grid-cols-2 gap-3"><button type="button" disabled={busy} onClick={() => setReferenceMigrationTarget(null)} className="rounded-xl border border-white/10 px-4 py-3 text-sm text-slate-300 hover:bg-white/5">取消</button><button type="button" disabled={busy || referenceMigrationConfirmation !== referenceMigrationTarget.name || !referenceFromReleaseId || !referenceToReleaseId} onClick={() => void migrateReferences()} className="rounded-xl bg-sky-400 px-4 py-3 text-sm font-semibold text-slate-950 disabled:opacity-35">生成迁移草稿</button></div>
          </div>
        </div>
      ) : null}
      {archiveTarget ? (
        <div className="fixed inset-0 z-[60] grid place-items-center bg-black/80 p-4 backdrop-blur-sm">
          <div role="alertdialog" aria-modal="true" aria-labelledby="archive-object-title" className="w-full max-w-lg rounded-3xl border border-white/10 bg-[#151923] p-6 shadow-2xl shadow-black/50">
            <div className="flex items-start justify-between gap-5">
              <div className="flex items-start gap-3">
                <div className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-slate-700/40 text-slate-200"><Archive size={19} /></div>
                <div><p className="text-xs font-medium uppercase tracking-[0.18em] text-slate-400">保留历史版本</p><h2 id="archive-object-title" className="mt-1 text-xl font-semibold">归档“{archiveTarget.name}”？</h2></div>
              </div>
              <button type="button" aria-label="关闭归档确认" onClick={() => { setArchiveTarget(null); setArchiveConfirmation(""); }} className="rounded-xl p-2 text-slate-500 hover:bg-white/5 hover:text-white"><X size={18} /></button>
            </div>
            <div className="mt-5 rounded-2xl border border-white/10 bg-white/[0.03] p-4 text-sm leading-6 text-slate-300">
              归档不会删除修订、发布版本或资产。对象将从默认列表隐藏；勾选“显示已归档对象”后可以恢复。若它是数据库运行时正在使用的当前对象，请先切换上游依赖或生效专家团。
            </div>
            <label className="mt-5 block text-sm text-slate-300">请输入对象名称 <strong className="text-white">{archiveTarget.name}</strong> 以确认
              <input autoFocus value={archiveConfirmation} onChange={(event) => setArchiveConfirmation(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && archiveConfirmation === archiveTarget.name && !busy) void archiveObject(); }} className="mt-2 w-full rounded-xl border border-white/10 bg-slate-950 px-3 py-3 outline-none focus:border-slate-400/50" />
            </label>
            <div className="mt-6 grid grid-cols-2 gap-3">
              <button type="button" disabled={busy} onClick={() => { setArchiveTarget(null); setArchiveConfirmation(""); }} className="rounded-xl border border-white/10 px-4 py-3 text-sm text-slate-300 hover:bg-white/5 disabled:opacity-40">取消</button>
              <button type="button" disabled={busy || archiveConfirmation !== archiveTarget.name} onClick={() => void archiveObject()} className="inline-flex items-center justify-center gap-2 rounded-xl bg-slate-200 px-4 py-3 text-sm font-semibold text-slate-950 hover:bg-white disabled:opacity-35"><Archive size={16} />确认归档</button>
            </div>
          </div>
        </div>
      ) : null}

      {deleteTarget ? (
        <div className="fixed inset-0 z-[60] grid place-items-center bg-black/80 p-4 backdrop-blur-sm">
          <div
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="delete-object-title"
            className="w-full max-w-lg rounded-3xl border border-rose-400/20 bg-[#151923] p-6 shadow-2xl shadow-black/50"
          >
            <div className="flex items-start justify-between gap-5">
              <div className="flex items-start gap-3">
                <div className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-rose-400/10 text-rose-300">
                  <Trash2 size={19} />
                </div>
                <div>
                  <p className="text-xs font-medium uppercase tracking-[0.18em] text-rose-300">
                    不可撤销操作
                  </p>
                  <h2
                    id="delete-object-title"
                    className="mt-1 text-xl font-semibold"
                  >
                    永久删除“{deleteTarget.name}”？
                  </h2>
                </div>
              </div>
              <button
                type="button"
                aria-label="关闭删除确认"
                onClick={() => {
                  setDeleteTarget(null);
                  setDeleteConfirmation("");
                }}
                className="rounded-xl p-2 text-slate-500 hover:bg-white/5 hover:text-white"
              >
                <X size={18} />
              </button>
            </div>
            <div className="mt-5 rounded-2xl border border-rose-400/15 bg-rose-400/[0.05] p-4 text-sm leading-6 text-rose-100/80">
              <p>
                删除后无法恢复。该对象的全部修订、发布版本、Python
                脚本、引用文档、调试记录和评测记录都会被永久清除。
              </p>
              <div className="mt-3 flex flex-wrap gap-2 text-xs">
                <StatusBadge tone="amber">
                  {TYPE_META[deleteTarget.object_type].label}
                </StatusBadge>
                <StatusBadge>
                  修订{" "}
                  {deleteTarget.revisions?.length ??
                    deleteTarget.latest_revision_no}
                </StatusBadge>
                <StatusBadge>
                  发布{" "}
                  {deleteTarget.releases?.length ??
                    deleteTarget.latest_release_no}
                </StatusBadge>
              </div>
              <p className="mt-3 text-xs text-rose-200/60">
                仅当其他专家或专家团的当前最新发布版本仍引用它时，系统才会阻止删除；历史版本、草稿和已固化生产部署不受影响。
              </p>
            </div>
            <label className="mt-5 block text-sm text-slate-300">
              请输入对象名称{" "}
              <strong className="text-white">{deleteTarget.name}</strong> 以确认
              <input
                autoFocus
                value={deleteConfirmation}
                onChange={(event) => setDeleteConfirmation(event.target.value)}
                onKeyDown={(event) => {
                  if (
                    event.key === "Enter" &&
                    deleteConfirmation === deleteTarget.name &&
                    !busy
                  )
                    void permanentlyDeleteObject();
                }}
                className="mt-2 w-full rounded-xl border border-white/10 bg-slate-950 px-3 py-3 outline-none focus:border-rose-400/50"
              />
            </label>
            <div className="mt-6 grid grid-cols-2 gap-3">
              <button
                type="button"
                disabled={busy}
                onClick={() => {
                  setDeleteTarget(null);
                  setDeleteConfirmation("");
                }}
                className="rounded-xl border border-white/10 px-4 py-3 text-sm text-slate-300 hover:bg-white/5 disabled:opacity-40"
              >
                取消
              </button>
              <button
                type="button"
                disabled={busy || deleteConfirmation !== deleteTarget.name}
                onClick={() => void permanentlyDeleteObject()}
                className="inline-flex items-center justify-center gap-2 rounded-xl bg-rose-500 px-4 py-3 text-sm font-semibold text-white hover:bg-rose-400 disabled:cursor-not-allowed disabled:opacity-35"
              >
                <Trash2 size={16} />
                确认永久删除
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {showCreate ? (
        <div className="fixed inset-0 z-50 grid place-items-center bg-black/70 p-4 backdrop-blur-sm">
          <div className="workbench-modal w-full max-w-xl rounded-3xl border border-white/10 bg-[#101b2b] p-6 shadow-2xl">
            <div className="flex items-center justify-between">
              <div>
                <h2 className="text-xl font-semibold">新建业务对象</h2>
                <p className="mt-1 text-sm text-slate-500">
                  创建后保存第一个不可变修订。
                </p>
              </div>
              <button
                type="button"
                onClick={() => setShowCreate(false)}
                className="rounded-xl p-2 text-slate-400 hover:bg-white/5"
              >
                <X size={18} />
              </button>
            </div>
            <div className="mt-6 grid grid-cols-3 gap-3">
              {(Object.keys(TYPE_META) as WorkbenchObjectType[]).map((type) => {
                const Icon = TYPE_META[type].icon;
                return (
                  <button
                    type="button"
                    key={type}
                    onClick={() =>
                      setNewObject({ ...newObject, object_type: type })
                    }
                    className={`rounded-2xl border p-4 text-left ${newObject.object_type === type ? "border-sky-400/40 bg-sky-400/10" : "border-white/10"}`}
                  >
                    <Icon size={17} className={TYPE_META[type].tone} />
                    <p className="mt-3 text-sm font-medium">
                      {TYPE_META[type].label}
                    </p>
                  </button>
                );
              })}
            </div>
            <div className="mt-5 grid gap-4">
              <label className="text-sm text-slate-300">
                显示名称
                <input
                  value={newObject.name}
                  onChange={(event) =>
                    setNewObject({ ...newObject, name: event.target.value })
                  }
                  className="mt-2 w-full rounded-xl border border-white/10 bg-slate-950 px-3 py-3 outline-none"
                />
              </label>
              <label className="text-sm text-slate-300">
                对象 ID
                <input
                  value={newObject.object_key}
                  onChange={(event) =>
                    setNewObject({
                      ...newObject,
                      object_key: event.target.value,
                    })
                  }
                  placeholder="lowercase_business_id"
                  className="mt-2 w-full rounded-xl border border-white/10 bg-slate-950 px-3 py-3 font-mono outline-none"
                />
              </label>
              <label className="text-sm text-slate-300">
                说明
                <textarea
                  value={newObject.description}
                  onChange={(event) =>
                    setNewObject({
                      ...newObject,
                      description: event.target.value,
                    })
                  }
                  rows={3}
                  className="mt-2 w-full rounded-xl border border-white/10 bg-slate-950 px-3 py-3 outline-none"
                />
              </label>
            </div>
            <button
              type="button"
              disabled={
                busy || !newObject.name.trim() || !newObject.object_key.trim()
              }
              onClick={() => void createObject()}
              className="mt-6 w-full rounded-xl bg-sky-400 px-4 py-3 font-semibold text-slate-950 disabled:opacity-40"
            >
              创建并进入编辑
            </button>
          </div>
        </div>
      ) : null}
    </main>
  );
}
