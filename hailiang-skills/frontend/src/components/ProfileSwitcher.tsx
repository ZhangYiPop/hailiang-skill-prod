import { useState } from "react";
import type { ProfileSummary } from "@/utils/api";

type ProfileSwitcherProps = {
  profiles: ProfileSummary[];
  activeProfileId: string;
  loading?: boolean;
  onSelect: (profileId: string) => void;
  onCreate: (input: { name: string; schoolYear: string; grade: string }) => void | Promise<void>;
};

export function ProfileSwitcher({
  profiles,
  activeProfileId,
  loading = false,
  onSelect,
  onCreate,
}: ProfileSwitcherProps) {
  const [isCreating, setIsCreating] = useState(false);
  const [draftName, setDraftName] = useState("");
  const [draftSchoolYear, setDraftSchoolYear] = useState("");
  const [draftGrade, setDraftGrade] = useState("");

  const startCreate = () => {
    setDraftName(`孩子 ${profiles.length + 1}`);
    setDraftSchoolYear("");
    setDraftGrade("");
    setIsCreating(true);
  };

  const submitCreate = async () => {
    const name = draftName.trim();
    const schoolYear = draftSchoolYear.trim();
    const grade = draftGrade.trim();
    if (!name || !schoolYear || !grade || loading) {
      return;
    }
    await onCreate({ name, schoolYear, grade });
    setDraftName("");
    setDraftSchoolYear("");
    setDraftGrade("");
    setIsCreating(false);
  };

  return (
    <section className="rounded-[28px] border border-white/10 bg-white/[0.04] p-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <p className="text-xs uppercase tracking-[0.18em] text-slate-500">孩子档案</p>
          <h3 className="mt-1 text-sm font-semibold text-white">切换当前孩子</h3>
        </div>
        <button
          type="button"
          onClick={startCreate}
          disabled={loading || isCreating}
          className="rounded-full border border-cyan-300/30 bg-cyan-300/10 px-3 py-2 text-xs text-cyan-50 transition hover:bg-cyan-300/20"
        >
          新建孩子
        </button>
      </div>

      {isCreating ? (
        <form
          className="mt-4 rounded-2xl border border-cyan-300/20 bg-cyan-300/10 p-3"
          onSubmit={(event) => {
            event.preventDefault();
            void submitCreate();
          }}
        >
          <label className="block text-xs uppercase tracking-[0.18em] text-cyan-100/70" htmlFor="new-profile-name">
            新孩子名称
          </label>
          <input
            id="new-profile-name"
            value={draftName}
            autoFocus
            disabled={loading}
            onChange={(event) => setDraftName(event.target.value)}
            className="mt-2 w-full rounded-2xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm text-white outline-none transition focus:border-cyan-300/40"
          />
          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            <label className="block text-xs uppercase tracking-[0.18em] text-cyan-100/70" htmlFor="new-profile-school-year">
              学年
              <input
                id="new-profile-school-year"
                value={draftSchoolYear}
                disabled={loading}
                onChange={(event) => setDraftSchoolYear(event.target.value)}
                placeholder="2026-2027"
                className="mt-2 w-full rounded-2xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm normal-case tracking-normal text-white outline-none transition focus:border-cyan-300/40"
              />
            </label>
            <label className="block text-xs uppercase tracking-[0.18em] text-cyan-100/70" htmlFor="new-profile-grade">
              年级
              <input
                id="new-profile-grade"
                value={draftGrade}
                disabled={loading}
                onChange={(event) => setDraftGrade(event.target.value)}
                placeholder="高一"
                className="mt-2 w-full rounded-2xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm normal-case tracking-normal text-white outline-none transition focus:border-cyan-300/40"
              />
            </label>
          </div>
          <div className="mt-3 flex justify-end gap-2">
            <button
              type="button"
              disabled={loading}
              onClick={() => {
                setDraftName("");
                setDraftSchoolYear("");
                setDraftGrade("");
                setIsCreating(false);
              }}
              className="rounded-full border border-white/10 px-3 py-1.5 text-xs text-slate-300 transition hover:border-white/20 hover:text-white"
            >
              取消
            </button>
            <button
              type="submit"
              disabled={loading || !draftName.trim() || !draftSchoolYear.trim() || !draftGrade.trim()}
              className="rounded-full border border-cyan-300/30 bg-cyan-300/15 px-3 py-1.5 text-xs text-cyan-50 transition hover:bg-cyan-300/25 disabled:cursor-not-allowed disabled:opacity-50"
            >
              创建
            </button>
          </div>
        </form>
      ) : null}

      <div className="mt-4 space-y-2">
        {profiles.length ? (
          profiles.map((profile) => {
            const active = profile.profile_id === activeProfileId;
            return (
              <button
                key={profile.profile_id}
                type="button"
                onClick={() => onSelect(profile.profile_id)}
                className={[
                  "flex w-full items-center justify-between rounded-2xl border px-4 py-3 text-left transition",
                  active
                    ? "border-cyan-300/40 bg-cyan-300/15 text-cyan-50"
                    : "border-white/10 bg-slate-950/70 text-slate-200 hover:border-cyan-300/25 hover:bg-cyan-300/10",
                ].join(" ")}
              >
                <span className="text-sm font-medium">{profile.name}</span>
                <span className="text-xs text-slate-400">{profile.is_default ? "默认" : "孩子"}</span>
              </button>
            );
          })
        ) : (
          <div className="rounded-2xl border border-dashed border-white/10 p-4 text-sm text-slate-400">
            {loading ? "正在初始化默认孩子档案..." : "当前还没有孩子档案，将自动创建默认孩子。"}
          </div>
        )}
      </div>
    </section>
  );
}
