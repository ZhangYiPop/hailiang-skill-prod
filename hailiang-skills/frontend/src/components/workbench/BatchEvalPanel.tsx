import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { Download, Square } from "lucide-react";

import { TESTER_MODEL_OPTIONS, testerModelName, useChatStore, type TesterModelId } from "@/store/useChatStore";
import { workbenchApi, type ObjectRevision, type SoulRevision, type WorkbenchObject, type WorkbenchObjectType } from "@/utils/workbenchApi";
import { casesFromRows, parseDelimited, parsePastedCases, rowsFromWorkbook, type BatchCaseDraft } from "./batchCases";

type BatchNode = { round: number; title: string; actor: string; status: string };
type BatchTurn = { role: string; content: string; source?: string };
type BatchCaseResult = BatchCaseDraft & {
  index: number;
  status: string;
  rounds: number;
  question_count: number;
  stop_reason: string;
  elapsed_s: number;
  experts: string[];
  result: string;
  nodes: BatchNode[];
  transcript: BatchTurn[];
  error: string;
};
type BatchJob = {
  job_id: string;
  status: string;
  total: number;
  finished: number;
  current_index: number;
  error: string;
  cases: BatchCaseResult[];
};

const TYPE_LABEL: Record<WorkbenchObjectType, string> = {
  skill: "Skill",
  expert: "专家",
  expert_team: "专家团",
};

const fieldClass = "w-full min-w-0 rounded-lg border border-white/10 bg-slate-950 px-2.5 py-2 text-sm outline-none focus:border-sky-400/50 disabled:opacity-40";

function formatRevision(revision: ObjectRevision): string {
  const when = new Date(revision.created_at).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
  return `r${revision.revision_no} · ${revision.created_by_display_name || revision.created_by || "未知"} · ${when} · ${revision.validation.valid ? "校验通过" : "待修复"}`;
}

async function readError(response: Response): Promise<string> {
  const payload = await response.json().catch(() => null) as { detail?: { message?: string } | string; message?: string } | null;
  if (typeof payload?.detail === "string") return payload.detail;
  return payload?.detail?.message || payload?.message || `HTTP ${response.status}`;
}

export function BatchEvalPanel({ baseUrl, actorId }: { baseUrl: string; actorId: string }) {
  const [objects, setObjects] = useState<WorkbenchObject[]>([]);
  const [souls, setSouls] = useState<SoulRevision[]>([]);
  const [typeFilter, setTypeFilter] = useState<WorkbenchObjectType | "all">("all");
  const [objectId, setObjectId] = useState("");
  const [detail, setDetail] = useState<WorkbenchObject | null>(null);
  const [revisionId, setRevisionId] = useState("");
  const [soulRevisionId, setSoulRevisionId] = useState("");
  const [personaMode, setPersonaMode] = useState<"simulator" | "seed">("simulator");
  const [speakerRole, setSpeakerRole] = useState<"家长" | "学生">("家长");
  const [modelId, setModelId] = useState<TesterModelId>(useChatStore.getState().selectedModelId);
  const [presetIdentity, setPresetIdentity] = useState(true);
  const [maxRounds, setMaxRounds] = useState(4);
  const [paste, setPaste] = useState("");
  const [cases, setCases] = useState<BatchCaseDraft[]>([]);
  const [job, setJob] = useState<BatchJob | null>(null);
  const [openIndex, setOpenIndex] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [importNote, setImportNote] = useState("");
  const [loadingObjects, setLoadingObjects] = useState(true);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const [listed, soulList] = await Promise.all([
          workbenchApi.listObjects(baseUrl),
          workbenchApi.listSoulRevisions(baseUrl),
        ]);
        if (cancelled) return;
        setObjects(listed.objects.filter((item) => !item.archived));
        setSouls(soulList.revisions);
      } catch (cause) {
        if (!cancelled) setError(cause instanceof Error ? cause.message : "读取调试台对象失败");
      } finally {
        if (!cancelled) setLoadingObjects(false);
      }
    })();
    return () => { cancelled = true; };
  }, [baseUrl]);

  const visibleObjects = useMemo(
    () => objects.filter((item) => typeFilter === "all" || item.object_type === typeFilter),
    [objects, typeFilter],
  );

  useEffect(() => {
    if (!objectId) {
      setDetail(null);
      setRevisionId("");
      return;
    }
    let cancelled = false;
    void workbenchApi.getObject(baseUrl, objectId).then((object) => {
      if (!cancelled) {
        setDetail(object);
        setRevisionId("");
      }
    }).catch((cause: unknown) => {
      if (!cancelled) setError(cause instanceof Error ? cause.message : "读取修订失败");
    });
    return () => { cancelled = true; };
  }, [baseUrl, objectId]);

  useEffect(() => {
    if (!job || job.status !== "running") return;
    const timer = window.setInterval(() => {
      void refresh(job.job_id);
    }, 2000);
    return () => window.clearInterval(timer);
  }, [job?.job_id, job?.status, baseUrl]);

  async function refresh(jobId: string) {
    const response = await fetch(`${baseUrl}/workbench/v1/batch-evals/${jobId}`);
    if (!response.ok) {
      setError(await readError(response));
      return;
    }
    setJob(await response.json() as BatchJob);
  }

  function importPaste() {
    const parsed = parsePastedCases(paste);
    if (!parsed.length) {
      setImportNote("");
      setError("没有解析到用例。请按「场景、首轮对话、画像、会话目标」四列粘贴，列之间用 Tab 分隔。");
      return;
    }
    applyImported(parsed);
  }

  function applyImported(parsed: BatchCaseDraft[], sourceCount = parsed.length) {
    if (!parsed.length) {
      setImportNote("");
      setError("没有读到用例。表格需要有首轮对话和画像，表头里带上「首轮」和「画像」即可。");
      return;
    }
    const limited = parsed.slice(0, 200);
    setError("");
    setCases(limited);
    setImportNote(sourceCount > 200 ? `表格共 ${sourceCount} 条，一次最多导入 200 条，已取前 200 条。` : `已导入 ${limited.length} 条用例。`);
  }

  async function importFile(file: File) {
    const name = file.name.toLowerCase();
    try {
      const rows = name.endsWith(".xlsx") || name.endsWith(".xls")
        ? await rowsFromWorkbook(await file.arrayBuffer())
        : parseDelimited(await file.text());
      applyImported(casesFromRows(rows));
    } catch (cause) {
      setImportNote("");
      setError(cause instanceof Error ? cause.message : "这个表格读不了，请另存为 .xlsx 或 .csv 后再导入。");
    }
  }

  function updateCase(index: number, patch: Partial<BatchCaseDraft>) {
    setCases((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, ...patch } : item));
  }

  async function start() {
    const ready = cases.filter((item) => item.opening.trim() && item.persona.trim());
    if (!revisionId || !ready.length) {
      setError("请先选择修订，并至少保留一条首轮对话和画像都完整的用例。");
      return;
    }
    setError("");
    const response = await fetch(`${baseUrl}/workbench/v1/batch-evals`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        revision_id: revisionId,
        soul_revision_id: soulRevisionId || null,
        actor_id: actorId,
        persona_mode: personaMode,
        speaker_role: speakerRole,
        model: modelId,
        preset_identity: presetIdentity,
        max_rounds: maxRounds,
        cases: ready.map((item) => ({
          scene: item.scene.trim(),
          opening: item.opening.trim(),
          persona: item.persona.trim(),
          goal: item.goal.trim(),
        })),
      }),
    });
    if (!response.ok) {
      setError(await readError(response));
      return;
    }
    setJob(await response.json() as BatchJob);
    setOpenIndex(null);
  }

  async function cancel() {
    if (!job) return;
    const response = await fetch(`${baseUrl}/workbench/v1/batch-evals/${job.job_id}/cancel`, { method: "POST" });
    if (!response.ok) {
      setError(await readError(response));
      return;
    }
    setJob(await response.json() as BatchJob);
  }

  async function exportTable() {
    if (!job) return;
    const response = await fetch(`${baseUrl}/workbench/v1/batch-evals/${job.job_id}/export`);
    if (!response.ok) {
      setError(await readError(response));
      return;
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `batch-eval-${job.job_id}.csv`;
    link.click();
    URL.revokeObjectURL(url);
  }

  const running = job?.status === "running";
  const progress = job ? Math.round((job.finished / Math.max(job.total, 1)) * 100) : 0;

  return (
    <section className="p-5 lg:p-8">
      <div className="mx-auto max-w-6xl space-y-6">
        <div className="rounded-3xl border border-white/10 bg-white/[0.025] p-6">
          <h2 className="text-lg font-semibold">选择调试台里的版本</h2>
          <p className="mt-2 text-sm leading-6 text-slate-400">
            对象、修订和 Soul 都来自调试台现有数据，和「调试与用例」用的是同一套快照。
          </p>
          <div className="mt-4 grid gap-3 lg:grid-cols-4">
            <select aria-label="对象类型" value={typeFilter} onChange={(event) => { setTypeFilter(event.target.value as WorkbenchObjectType | "all"); setObjectId(""); }} className={fieldClass}>
              <option value="all">全部类型</option>
              <option value="expert_team">专家团</option>
              <option value="expert">专家</option>
              <option value="skill">Skill</option>
            </select>
            <select aria-label="测试对象" value={objectId} disabled={loadingObjects || !visibleObjects.length} onChange={(event) => setObjectId(event.target.value)} className={fieldClass}>
              <option value="">{loadingObjects ? "正在读取对象…" : "请选择对象"}</option>
              {visibleObjects.map((item) => (
                <option key={item.object_id} value={item.object_id}>{TYPE_LABEL[item.object_type]} · {item.name} · {item.object_key}</option>
              ))}
            </select>
            <select aria-label="修订版本" value={revisionId} disabled={!detail?.revisions?.length || running} onChange={(event) => setRevisionId(event.target.value)} className={fieldClass}>
              <option value="">请选择一个修订版本</option>
              {(detail?.revisions ?? []).map((revision) => (
                <option key={revision.revision_id} value={revision.revision_id}>{formatRevision(revision)}</option>
              ))}
            </select>
            <select aria-label="Soul 修订" value={soulRevisionId} disabled={running} onChange={(event) => setSoulRevisionId(event.target.value)} className={fieldClass}>
              <option value="">不加载 Soul</option>
              {souls.map((item) => (
                <option key={item.soul_revision_id} value={item.soul_revision_id}>Soul r{item.revision_no}</option>
              ))}
            </select>
          </div>
        </div>

        <div className="rounded-3xl border border-white/10 bg-white/[0.025] p-6">
          <h2 className="text-lg font-semibold">需要手动补充的内容</h2>
          <div className="mt-4 grid gap-3 lg:grid-cols-4">
            <label className="text-sm text-slate-400">
              画像用法
              <select aria-label="画像用法" value={personaMode} disabled={running} onChange={(event) => setPersonaMode(event.target.value === "seed" ? "seed" : "simulator")} className={`mt-1.5 ${fieldClass}`}>
                <option value="simulator">只作模拟用户背景</option>
                <option value="seed">同时预填系统事实</option>
              </select>
            </label>
            <label className="text-sm text-slate-400">
              发言身份
              <select aria-label="发言身份" value={speakerRole} disabled={running} onChange={(event) => setSpeakerRole(event.target.value === "学生" ? "学生" : "家长")} className={`mt-1.5 ${fieldClass}`}>
                <option value="家长">家长</option>
                <option value="学生">孩子</option>
              </select>
            </label>
            <label className="text-sm text-slate-400">
              问答模型
              <select aria-label="问答模型" title={testerModelName(modelId)} value={modelId} disabled={running} onChange={(event) => setModelId(event.target.value === "pro" ? "pro" : "flash")} className={`mt-1.5 ${fieldClass}`}>
                {TESTER_MODEL_OPTIONS.map((item) => (
                  <option key={item.id} value={item.id}>{item.model}</option>
                ))}
              </select>
            </label>
            <label className="text-sm text-slate-400">
              每个用例最多轮数
              <input aria-label="最大轮数" type="number" min={1} max={8} value={maxRounds} disabled={running} onChange={(event) => setMaxRounds(Math.min(8, Math.max(1, Number(event.target.value) || 1)))} className={`mt-1.5 ${fieldClass}`} />
            </label>
          </div>
          <p className="mt-2 text-sm leading-6 text-slate-500">画像写的是孩子，发言身份默认家长；选孩子时系统身份记为学生。问答模型和调试台是同一组。</p>
          <label className="mt-4 flex items-start gap-3 text-sm text-slate-300">
            <input type="checkbox" className="mt-1 accent-sky-400" checked={presetIdentity} disabled={running} onChange={(event) => setPresetIdentity(event.target.checked)} />
            <span>预填所选身份和画像里的学段。调试台会话的身份检查不会弹出表单，不预填时缺身份会直接结束本轮。</span>
          </label>
          <label className="mt-4 block text-sm text-slate-300">
            从评测表粘贴用例
            <textarea value={paste} disabled={running} onChange={(event) => setPaste(event.target.value)} rows={3} placeholder={"场景\t首轮对话\t画像信息\t会话目标"} className={`mt-1.5 ${fieldClass}`} />
          </label>
          <div className="mt-3 flex flex-wrap items-center gap-3">
            <button type="button" disabled={running || !paste.trim()} onClick={importPaste} className="rounded-xl border border-white/10 px-4 py-2 text-sm text-slate-200 disabled:opacity-40">解析到用例表</button>
            <button type="button" disabled={running} onClick={() => fileRef.current?.click()} className="rounded-xl border border-white/10 px-4 py-2 text-sm text-slate-200 disabled:opacity-40">导入表格</button>
            <input ref={fileRef} type="file" accept=".csv,.tsv,.txt,.xlsx,.xls,text/csv,application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" className="hidden" onChange={(event) => {
              const file = event.target.files?.[0];
              event.target.value = "";
              if (file) void importFile(file);
            }} />
            <span className="text-xs text-slate-500">支持 .xlsx、.xls、.csv。表头包含「首轮」和「画像」即可。</span>
          </div>
          {importNote ? <p className="mt-3 text-sm text-slate-300">{importNote}</p> : null}
          {cases.length ? (
            <div className="mt-4 max-h-[28rem] space-y-3 overflow-auto">
              {cases.map((item, index) => (
                <div key={`${item.opening}-${index}`} className="rounded-2xl border border-white/10 bg-slate-950/50 p-3">
                  <div className="mb-2 flex items-center justify-between text-xs text-slate-500">
                    <span>用例 {index + 1}</span>
                    <button type="button" disabled={running} onClick={() => { setCases((current) => current.filter((_, itemIndex) => itemIndex !== index)); setImportNote(""); }} className="text-rose-300 disabled:opacity-40">删除</button>
                  </div>
                  <div className="grid gap-2 md:grid-cols-2">
                    <input aria-label={`场景 ${index + 1}`} value={item.scene} disabled={running} onChange={(event) => updateCase(index, { scene: event.target.value })} placeholder="场景" className={fieldClass} />
                    <input aria-label={`首轮 ${index + 1}`} value={item.opening} disabled={running} onChange={(event) => updateCase(index, { opening: event.target.value })} placeholder="首轮对话" className={fieldClass} />
                  </div>
                  <textarea aria-label={`画像 ${index + 1}`} value={item.persona} disabled={running} onChange={(event) => updateCase(index, { persona: event.target.value })} rows={2} placeholder="画像信息" className={`mt-2 ${fieldClass}`} />
                  <input aria-label={`目标 ${index + 1}`} value={item.goal} disabled={running} onChange={(event) => updateCase(index, { goal: event.target.value })} placeholder="会话目标，可空" className={`mt-2 ${fieldClass}`} />
                </div>
              ))}
            </div>
          ) : null}
          <button type="button" disabled={running || !revisionId || !cases.length} onClick={() => void start()} className="mt-4 rounded-xl bg-sky-400 px-4 py-2.5 text-sm font-semibold text-slate-950 disabled:opacity-40">
            开始批量测评（{cases.length} 条）
          </button>
        </div>

        {error ? <p className="rounded-2xl border border-rose-400/20 bg-rose-400/10 px-4 py-3 text-sm text-rose-100">{error}</p> : null}

        {job ? (
          <div className="rounded-3xl border border-white/10 bg-white/[0.025] p-6">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <h2 className="text-lg font-semibold">运行进度</h2>
                <p className="mt-1 text-sm text-slate-400">{job.finished}/{job.total} · {job.status === "running" ? `正在跑第 ${job.current_index} 条` : job.status === "cancelled" ? "已停止" : job.status === "failed" ? "运行失败" : "已完成"}</p>
              </div>
              <div className="flex gap-2">
                <button type="button" disabled={!running} onClick={() => void cancel()} className="inline-flex items-center gap-1.5 rounded-xl border border-white/10 px-3 py-2 text-sm text-slate-200 disabled:opacity-40"><Square size={14} />停止</button>
                <button type="button" onClick={() => void exportTable()} className="inline-flex items-center gap-1.5 rounded-xl border border-sky-400/30 px-3 py-2 text-sm text-sky-100"><Download size={14} />导出表格</button>
              </div>
            </div>
            <div className="mt-4 h-2 overflow-hidden rounded-full bg-white/10">
              <div className="h-full bg-sky-400" style={{ width: `${progress}%` }} />
            </div>
            {job.error ? <p className="mt-3 text-sm text-rose-200">{job.error}</p> : null}
            <div className="mt-4 overflow-auto">
              <table className="w-full min-w-[720px] text-left text-sm">
                <thead className="text-xs text-slate-500">
                  <tr>
                    <th className="px-2 py-2 font-medium">场景</th>
                    <th className="px-2 py-2 font-medium">状态</th>
                    <th className="px-2 py-2 font-medium">轮数</th>
                    <th className="px-2 py-2 font-medium">追问</th>
                    <th className="px-2 py-2 font-medium">结束原因</th>
                    <th className="px-2 py-2 font-medium">最终回复</th>
                  </tr>
                </thead>
                <tbody>
                  {job.cases.map((item) => (
                    <Fragment key={item.index}>
                      <tr className="border-t border-white/10 align-top">
                        <td className="px-2 py-3">{item.scene || item.opening.slice(0, 18)}</td>
                        <td className="px-2 py-3">{item.status === "running" ? "进行中" : item.status === "completed" ? "完成" : item.status === "failed" ? "失败" : item.status === "cancelled" ? "已停止" : "等待"}</td>
                        <td className="px-2 py-3">{item.rounds}</td>
                        <td className="px-2 py-3">{item.question_count}</td>
                        <td className="px-2 py-3 text-slate-400">{item.stop_reason}</td>
                        <td className="px-2 py-3">
                          <p className="line-clamp-2 text-slate-300">{item.result || item.error || "—"}</p>
                          <button type="button" onClick={() => setOpenIndex(openIndex === item.index ? null : item.index)} className="mt-1 text-xs text-sky-300">
                            {openIndex === item.index ? "收起过程" : "查看过程和对话"}
                          </button>
                        </td>
                      </tr>
                      {openIndex === item.index ? (
                        <tr key={`${item.index}-detail`} className="border-t border-white/5">
                          <td colSpan={6} className="px-2 py-3">
                            <p className="text-xs text-slate-500">节点</p>
                            <ul className="mt-1 space-y-1 text-xs text-slate-300">
                              {item.nodes.length ? item.nodes.map((node, nodeIndex) => (
                                <li key={`${item.index}-${nodeIndex}`}>第{node.round}轮 · {node.actor || "系统"} · {node.title} · {node.status}</li>
                              )) : <li>还没有节点</li>}
                            </ul>
                            <p className="mt-3 text-xs text-slate-500">对话</p>
                            <div className="mt-1 space-y-2">
                              {item.transcript.map((turn, turnIndex) => (
                                <p key={`${item.index}-${turnIndex}`} className="text-xs leading-5 text-slate-300">
                                  <span className="text-slate-500">{turn.role === "assistant" ? "助手" : "用户"}{turn.source ? `（${turn.source}）` : ""}：</span>
                                  {turn.content}
                                </p>
                              ))}
                            </div>
                          </td>
                        </tr>
                      ) : null}
                    </Fragment>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ) : null}
      </div>
    </section>
  );
}
