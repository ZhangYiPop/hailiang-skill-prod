import { useState } from "react";

import type { DebugIdentity } from "@/utils/api";

type Props = {
  identity: DebugIdentity | null;
  onApply: (identity: DebugIdentity, options?: { refreshProfileWorkspace?: boolean }) => Promise<void>;
  onReset: () => Promise<void>;
};

type IdentityMode = "local_debug" | "forwarding";

function makeGeneratedId(prefix: string): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return `${prefix}_${crypto.randomUUID()}`;
  }
  return `${prefix}_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
}

export function DebugIdentityPanel({ identity, onApply, onReset }: Props) {
  const [mode, setMode] = useState<IdentityMode>(identity?.profile_id && identity?.session_id ? "forwarding" : "local_debug");
  const [userId, setUserId] = useState(identity?.user_id ?? "");
  const [studentName, setStudentName] = useState(identity?.display_name ?? "");
  const [profileId, setProfileId] = useState(identity?.profile_id ?? "");
  const [sessionId, setSessionId] = useState(identity?.session_id ?? "");
  const [schoolYear, setSchoolYear] = useState(identity?.school_year ?? "");
  const [grade, setGrade] = useState(identity?.grade ?? "");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ready = mode === "local_debug"
    ? [userId, studentName, schoolYear, grade].every((value) => value.trim())
    : Boolean(userId.trim());
  const field = (label: string, value: string, onChange: (value: string) => void, placeholder: string) => (
    <label className="block"><span className="mb-1 block text-xs text-slate-400">{label}</span><input value={value} onChange={(event) => onChange(event.target.value)} placeholder={placeholder} className="w-full rounded-xl border border-white/10 bg-slate-950/80 px-3 py-2 text-sm text-white outline-none focus:border-cyan-300/40" /></label>
  );
  return <section className="rounded-[28px] border border-white/10 bg-white/[0.04] p-4">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <div>
        <p className="text-xs uppercase tracking-[0.18em] text-slate-500">测试工作区</p>
        <h3 className="mt-1 text-sm font-semibold text-white">身份接入模式</h3>
      </div>
      <div className="flex rounded-full border border-white/10 bg-slate-950/70 p-1">
        {([
          ["local_debug", "本地调试身份"],
          ["forwarding", "转发请求数据"],
        ] as const).map(([value, label]) => (
          <button
            key={value}
            type="button"
            onClick={() => {
              setMode(value);
              setError(null);
              if (value === "local_debug") {
                // Local debugging uses the same BFF request contract while
                // generating opaque IDs for a fresh development session.
                setProfileId(makeGeneratedId("prof_debug"));
                setSessionId(makeGeneratedId("sess_debug"));
              }
            }}
            className={["rounded-full px-3 py-1.5 text-xs transition", mode === value ? "bg-cyan-300/20 text-cyan-50" : "text-slate-400 hover:text-slate-200"].join(" ")}
          >
            {label}
          </button>
        ))}
      </div>
    </div>
    {mode === "local_debug" ? (
      <p className="mt-2 text-xs leading-5 text-slate-400">只需填写用户 ID、孩子姓名和学年/年级；点击“进入本地调试工作区”时自动生成孩子 ID 和会话 ID。</p>
    ) : (
      <p className="mt-2 text-xs leading-5 text-slate-400">应用时会重新读取服务端孩子、Facts 和会话。有效孩子 ID 优先；未填写或无效时自动选择第一个孩子。</p>
    )}
    <form className="mt-4 space-y-3" onSubmit={async (event) => {
      event.preventDefault();
      setSubmitting(true);
      setError(null);
      try {
        await onApply({
          user_id: userId.trim(),
          display_name: studentName.trim(),
          profile_id: mode === "local_debug" ? (profileId.trim() || makeGeneratedId("prof_debug")) : profileId.trim(),
          session_id: mode === "local_debug" ? (sessionId.trim() || makeGeneratedId("sess_debug")) : sessionId.trim(),
          school_year: schoolYear.trim(),
          grade: grade.trim(),
        }, { refreshProfileWorkspace: mode === "forwarding" });
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : "保存调试身份失败");
      } finally {
        setSubmitting(false);
      }
    }}>
      {field("用户 ID", userId, setUserId, "user_001")}
      {field(mode === "forwarding" ? "孩子姓名（历史上下文，可空）" : "孩子姓名", studentName, setStudentName, "张三")}
      {field(mode === "forwarding" ? "学年（历史上下文，可空）" : "学年", schoolYear, setSchoolYear, "2026-2027")}
      {field(mode === "forwarding" ? "年级（历史上下文，可空）" : "年级", grade, setGrade, "高一")}
      {mode === "forwarding" ? <>
        {field("孩子 ID（可空）", profileId, setProfileId, "child_001")}
        {field("会话 ID（可空）", sessionId, setSessionId, "sess_001")}
      </> : null}
      {mode === "local_debug" && identity?.profile_id && identity?.session_id ? <p className="rounded-xl border border-emerald-300/20 bg-emerald-300/5 px-3 py-2 text-xs leading-5 text-emerald-100">当前工作区已生成：孩子 ID <span className="font-mono">{identity.profile_id}</span>，会话 ID <span className="font-mono">{identity.session_id}</span></p> : null}
      <div className="flex flex-wrap gap-2"><button type="submit" disabled={submitting || !ready} className="rounded-xl border border-cyan-300/40 bg-cyan-300/15 px-3 py-2 text-xs text-cyan-50 disabled:opacity-40">{submitting ? "处理中..." : mode === "local_debug" ? "进入本地调试工作区" : "应用转发调试身份"}</button><button type="button" onClick={() => void onReset()} className="rounded-xl border border-white/10 px-3 py-2 text-xs text-slate-300">清空工作区</button></div>
      {error ? <p className="text-xs text-rose-300">{error}</p> : null}
    </form>
  </section>;
}
