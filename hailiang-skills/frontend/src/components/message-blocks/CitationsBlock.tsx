import { useMemo, useState } from "react";

import type { CitationGroup, CitationItem, CitationsBlock as CitationsBlockType } from "@/types/messageBlocks";

type CitationsBlockProps = {
  block: CitationsBlockType;
};

function getGroupLabel(group: CitationGroup) {
  return group.label ?? (group.kind === "fact" ? "Fact" : "Asset");
}

function getItemTitle(item: CitationItem, fallbackKind: string) {
  return item.title ?? `${fallbackKind} detail`;
}

export function CitationsBlock({ block }: CitationsBlockProps) {
  const groups = useMemo(
    () => (block.payload.groups ?? []).filter((group) => (group.items ?? []).length > 0),
    [block.payload.groups],
  );
  const [activeKind, setActiveKind] = useState<string>("");
  const [expandedKeys, setExpandedKeys] = useState<string[]>([]);

  if (!groups.length) {
    return null;
  }

  const activeGroup = groups.find((group) => group.kind === activeKind) ?? null;

  return (
    <>
      <div className="rounded-2xl border border-white/10 bg-white/[0.03] p-4">
        <p className="text-xs uppercase tracking-[0.18em] text-slate-500">引用与来源</p>
        <div className="mt-3 flex flex-wrap gap-2">
          {groups.map((group) => (
            <button
              key={group.kind}
              type="button"
              onClick={() => setActiveKind(group.kind)}
              className="rounded-full border border-white/10 bg-white/[0.04] px-3 py-2 text-xs text-slate-300 transition hover:border-cyan-300/25 hover:bg-cyan-300/10"
            >
              {getGroupLabel(group)} {group.items.length}
            </button>
          ))}
        </div>
      </div>
      {activeGroup ? (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/80 p-4">
          <div className="max-h-[80vh] w-full max-w-3xl overflow-auto rounded-[28px] border border-white/10 bg-[#07111f] p-6 shadow-[0_30px_120px_rgba(0,0,0,0.45)]">
            <div className="flex items-start justify-between gap-4">
              <div>
                <p className="text-xs uppercase tracking-[0.18em] text-slate-500">引用与来源</p>
                <h4 className="mt-2 text-xl font-semibold text-white">
                  {getGroupLabel(activeGroup)} 详情
                </h4>
              </div>
              <button
                type="button"
                onClick={() => {
                  setActiveKind("");
                  setExpandedKeys([]);
                }}
                className="rounded-full border border-white/10 px-3 py-2 text-xs text-slate-300 transition hover:border-cyan-300/30 hover:text-cyan-100"
              >
                关闭
              </button>
            </div>
            <div className="mt-5 space-y-3">
              {activeGroup.items.map((item, index) => {
                const itemKey = `${activeGroup.kind}-${getItemTitle(item, activeGroup.kind)}-${index}`;
                const expanded = expandedKeys.includes(itemKey);
                const detailText = JSON.stringify(item.detail ?? {}, null, 2);
                const shouldCollapse = detailText.length > 280;
                return (
                  <div
                    key={itemKey}
                    className="rounded-2xl border border-white/10 bg-slate-950/70 px-4 py-4"
                  >
                    <p className="text-sm font-medium text-white">{getItemTitle(item, activeGroup.kind)}</p>
                    <p className="mt-1 text-xs leading-5 text-slate-400">
                      {item.summary || "详细来源信息"}
                    </p>
                    <pre className="mt-3 whitespace-pre-wrap break-all rounded-2xl border border-white/10 bg-slate-950/80 p-4 text-xs leading-6 text-slate-200">
                      {shouldCollapse && !expanded ? `${detailText.slice(0, 280)}...` : detailText}
                    </pre>
                    {shouldCollapse ? (
                      <button
                        type="button"
                        onClick={() =>
                          setExpandedKeys((current) =>
                            current.includes(itemKey)
                              ? current.filter((key) => key !== itemKey)
                              : [...current, itemKey],
                          )
                        }
                        className="mt-3 rounded-full border border-white/10 px-3 py-2 text-xs text-cyan-100 transition hover:border-cyan-300/30 hover:bg-cyan-300/10"
                      >
                        {expanded ? "收起" : "更多"}
                      </button>
                    ) : null}
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      ) : null}
    </>
  );
}
