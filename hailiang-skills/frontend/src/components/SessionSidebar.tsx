import { useState } from "react";

import type { SessionListItem } from "@/utils/api";

type SessionSidebarProps = {
  sessions: SessionListItem[];
  activeSessionId: string;
  activeProfileName?: string;
  loading: boolean;
  onSelect: (sessionId: string) => void;
  onRename: (sessionId: string, title: string) => Promise<void>;
  onDelete: (sessionId: string) => Promise<void>;
};

export function SessionSidebar({
  sessions,
  activeSessionId,
  activeProfileName,
  loading,
  onSelect,
  onRename,
  onDelete,
}: SessionSidebarProps) {
  const [editingSessionId, setEditingSessionId] = useState("");
  const [draftTitle, setDraftTitle] = useState("");

  return (
    <section className="rounded-[28px] border border-white/10 bg-white/[0.04] p-4">
      <div>
        <p className="text-xs uppercase tracking-[0.18em] text-slate-500">历史会话</p>
        <h3 className="mt-1 text-sm font-semibold text-white">当前孩子的聊天记录</h3>
        <p className="mt-2 text-xs leading-5 text-slate-400">
          {activeProfileName
            ? `当前档案：${activeProfileName}，点击任一会话可恢复聊天记录与状态。`
            : "先选择一个孩子档案，再查看该孩子的历史会话。"}
        </p>
      </div>

      <div className="mt-4 space-y-3">
        {loading ? (
          <div className="rounded-2xl border border-white/10 bg-slate-950/70 p-4 text-sm text-slate-300">
            正在加载历史会话...
          </div>
        ) : sessions.length ? (
          sessions.map((session) => {
            const active = session.session_id === activeSessionId;
            const isEditing = editingSessionId === session.session_id;
            return (
              <div
                key={session.session_id}
                className={[
                  "rounded-2xl border p-4 transition",
                  active
                    ? "border-cyan-300/40 bg-cyan-300/10"
                    : "border-white/10 bg-slate-950/70",
                ].join(" ")}
              >
                <button
                  type="button"
                  onClick={() => onSelect(session.session_id)}
                  className="w-full text-left"
                >
                  <p className="text-sm font-semibold text-white">
                    {session.title?.trim() || "未命名会话"}
                  </p>
                  <p className="mt-1 text-xs text-slate-500">
                    {session.message_count} 条消息
                    {session.active_skill ? ` · ${session.active_skill}` : ""}
                  </p>
                </button>
                {active ? (
                  <p className="mt-3 text-xs text-cyan-200">当前正在查看这个会话</p>
                ) : null}
                {isEditing ? (
                  <div className="mt-3 flex gap-2">
                    <input
                      value={draftTitle}
                      onChange={(event) => setDraftTitle(event.target.value)}
                      className="flex-1 rounded-2xl border border-white/10 bg-slate-950/80 px-3 py-2 text-sm text-white outline-none"
                    />
                    <button
                      type="button"
                      onClick={async () => {
                        if (!draftTitle.trim()) {
                          return;
                        }
                        await onRename(session.session_id, draftTitle.trim());
                        setEditingSessionId("");
                        setDraftTitle("");
                      }}
                      className="rounded-2xl border border-cyan-300/30 bg-cyan-300/10 px-3 py-2 text-xs text-cyan-50"
                    >
                      保存
                    </button>
                  </div>
                ) : (
                  <div className="mt-3 flex items-center gap-3">
                    <button
                      type="button"
                      onClick={() => {
                        setEditingSessionId(session.session_id);
                        setDraftTitle(session.title || "");
                      }}
                      className="text-xs text-cyan-200 transition hover:text-cyan-100"
                    >
                      重命名标题
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        if (window.confirm(`确认删除“${session.title?.trim() || "未命名会话"}”吗？此操作无法恢复。`)) {
                          void onDelete(session.session_id);
                        }
                      }}
                      className="text-xs text-rose-300 transition hover:text-rose-200"
                    >
                      删除
                    </button>
                  </div>
                )}
              </div>
            );
          })
        ) : (
          <div className="rounded-2xl border border-dashed border-white/10 p-4 text-sm text-slate-400">
            {activeProfileName ? `${activeProfileName} 还没有历史会话。` : "当前孩子还没有历史会话。"}
          </div>
        )}
      </div>
    </section>
  );
}
