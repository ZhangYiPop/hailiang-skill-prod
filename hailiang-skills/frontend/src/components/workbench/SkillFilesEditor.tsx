import { useEffect, useMemo, useState } from "react";
import {
  Code2,
  Download,
  FileText,
  Plus,
  ShieldCheck,
  Trash2,
  Upload,
} from "lucide-react";

export type EditableSkillFile = {
  relative_path: string;
  media_type: string;
  content_base64: string;
  size: number;
};

type SkillFileKind = "reference" | "asset" | "script";

type Props = {
  files: EditableSkillFile[];
  onChange: (files: EditableSkillFile[]) => void;
  onUpload: (files: FileList | null, kind: "reference" | "asset") => Promise<void>;
};

function decodeText(value: string): string {
  try {
    const binary = atob(value);
    const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    return "";
  }
}

function encodeText(value: string): string {
  const bytes = new TextEncoder().encode(value);
  let binary = "";
  for (let index = 0; index < bytes.length; index += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000));
  }
  return btoa(binary);
}

function isEditable(file: EditableSkillFile): boolean {
  return (
    file.relative_path.endsWith(".py") ||
    file.relative_path.endsWith(".md") ||
    file.relative_path.endsWith(".txt") ||
    file.relative_path.endsWith(".json") ||
    file.relative_path.endsWith(".yaml") ||
    file.relative_path.endsWith(".yml") ||
    file.media_type.startsWith("text/")
  );
}

export function SkillFilesEditor({ files, onChange, onUpload }: Props) {
  const managedFiles = useMemo(
    () =>
      files.filter(
        (file) =>
          file.relative_path.startsWith("references/") ||
          file.relative_path.startsWith("assets/") ||
          file.relative_path.startsWith("scripts/"),
      ),
    [files],
  );
  const [selectedPath, setSelectedPath] = useState("");
  const [newName, setNewName] = useState("");
  const [newKind, setNewKind] = useState<SkillFileKind>("reference");
  const selected =
    managedFiles.find((file) => file.relative_path === selectedPath) ?? null;

  useEffect(() => {
    if (!managedFiles.length) {
      setSelectedPath("");
    } else if (
      !managedFiles.some((file) => file.relative_path === selectedPath)
    ) {
      setSelectedPath(managedFiles[0].relative_path);
    }
  }, [managedFiles, selectedPath]);

  function addTextFile() {
    const raw = newName.trim().replace(/^\/+/, "").replace(/\\/g, "/");
    if (!raw || raw.includes("..")) return;
    const prefix = newKind === "script" ? "scripts/" : newKind === "asset" ? "assets/" : "references/";
    const extension = newKind === "script" ? ".py" : newKind === "asset" ? ".json" : ".md";
    const leaf = raw.startsWith(prefix) ? raw.slice(prefix.length) : raw;
    const relativePath = `${prefix}${leaf.includes(".") ? leaf : `${leaf}${extension}`}`;
    if (files.some((file) => file.relative_path === relativePath)) {
      setSelectedPath(relativePath);
      return;
    }
    const initial =
      newKind === "script"
        ? 'from __future__ import annotations\n\n\ndef main(payload: dict) -> dict:\n    return {"ok": True, "payload": payload}\n'
        : newKind === "asset"
          ? "{}\n"
          : `# ${leaf.replace(/\.[^.]+$/, "")}\n\n`;
    const next = {
      relative_path: relativePath,
      media_type: newKind === "script" ? "text/x-python" : newKind === "asset" ? "application/json" : "text/markdown",
      content_base64: encodeText(initial),
      size: new TextEncoder().encode(initial).length,
    };
    onChange([...files, next]);
    setSelectedPath(relativePath);
    setNewName("");
  }

  function updateSelected(text: string) {
    if (!selected) return;
    onChange(
      files.map((file) =>
        file.relative_path === selected.relative_path
          ? {
              ...file,
              content_base64: encodeText(text),
              size: new TextEncoder().encode(text).length,
            }
          : file,
      ),
    );
  }

  function removeSelected() {
    if (!selected) return;
    onChange(
      files.filter((file) => file.relative_path !== selected.relative_path),
    );
  }

  function downloadSelected() {
    if (!selected) return;
    const binary = atob(selected.content_base64);
    const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
    const url = URL.createObjectURL(
      new Blob([bytes], { type: selected.media_type }),
    );
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = selected.relative_path.split("/").at(-1) ?? "file";
    anchor.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="rounded-3xl border border-white/10 bg-white/[0.025] p-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <Code2 size={17} className="text-cyan-300" />
            <h3 className="font-medium">Python 脚本、引用文档与本地 assets</h3>
          </div>
          <p className="mt-1 text-sm text-slate-500">
            文件随修订保存。assets 用于 Skill 本地数据；Python 发布前会检查语法、依赖和危险调用，并只在沙箱执行。
          </p>
        </div>
        <label className="cursor-pointer rounded-xl border border-white/10 px-3 py-2 text-xs text-slate-300 hover:text-white">
          <Upload className="mr-1.5 inline" size={14} />
          {newKind === "asset" ? "上传本地 asset" : "上传引用文档"}
          <input
            type="file"
            multiple
            className="hidden"
            onChange={(event) => {
              void onUpload(event.target.files, newKind === "asset" ? "asset" : "reference");
              event.currentTarget.value = "";
            }}
          />
        </label>
      </div>

      <div className="mt-5 grid overflow-hidden rounded-2xl border border-white/10 lg:grid-cols-[260px_minmax(0,1fr)]">
        <aside className="border-b border-white/10 bg-slate-950/45 p-3 lg:border-b-0 lg:border-r">
          <div className="grid grid-cols-[96px_minmax(0,1fr)_36px] gap-2">
            <select
              value={newKind}
              onChange={(event) =>
                setNewKind(event.target.value as SkillFileKind)
              }
              className="rounded-lg border border-white/10 bg-slate-900 px-2 text-xs outline-none"
            >
              <option value="reference">引用</option>
              <option value="asset">本地 asset</option>
              <option value="script">Python</option>
            </select>
            <input
              value={newName}
              onChange={(event) => setNewName(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") addTextFile();
              }}
              placeholder={newKind === "script" ? "score.py" : newKind === "asset" ? "题库.json" : "规则说明.md"}
              className="min-w-0 rounded-lg border border-white/10 bg-slate-900 px-2.5 py-2 text-xs outline-none focus:border-cyan-400/40"
            />
            <button
              type="button"
              onClick={addTextFile}
              aria-label="新建文件"
              className="grid place-items-center rounded-lg bg-cyan-400 text-slate-950 disabled:opacity-40"
              disabled={!newName.trim()}
            >
              <Plus size={15} />
            </button>
          </div>
          <div className="mt-3 max-h-[430px] space-y-1 overflow-auto">
            {managedFiles.map((file) => {
              const script = file.relative_path.startsWith("scripts/");
              const asset = file.relative_path.startsWith("assets/");
              return (
                <button
                  key={file.relative_path}
                  type="button"
                  onClick={() => setSelectedPath(file.relative_path)}
                  className={`flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-xs ${selectedPath === file.relative_path ? "bg-cyan-400/10 text-cyan-100" : "text-slate-400 hover:bg-white/5 hover:text-slate-200"}`}
                >
                  {script ? (
                    <Code2 size={14} className="shrink-0 text-violet-300" />
                  ) : (
                    <FileText size={14} className={`shrink-0 ${asset ? "text-amber-300" : "text-sky-300"}`} />
                  )}
                  <span className="min-w-0 truncate">{file.relative_path}</span>
                </button>
              );
            })}
            {!managedFiles.length ? (
              <p className="px-2 py-6 text-center text-xs text-slate-600">
                还没有脚本、引用文档或本地 assets
              </p>
            ) : null}
          </div>
        </aside>
        <div className="min-h-[360px] bg-slate-950/65">
          {selected ? (
            <>
              <div className="flex items-center justify-between border-b border-white/10 px-4 py-3">
                <div>
                  <p className="font-mono text-xs text-slate-300">
                    {selected.relative_path}
                  </p>
                  <p className="mt-1 text-[10px] text-slate-600">
                    {(selected.size / 1024).toFixed(1)} KB
                  </p>
                </div>
                <div className="flex items-center gap-1">
                  <button
                    type="button"
                    onClick={downloadSelected}
                    aria-label="下载文件"
                    className="rounded-lg p-2 text-slate-500 hover:bg-white/5 hover:text-white"
                  >
                    <Download size={15} />
                  </button>
                  <button
                    type="button"
                    onClick={removeSelected}
                    aria-label="删除文件"
                    className="rounded-lg p-2 text-slate-500 hover:bg-rose-400/10 hover:text-rose-300"
                  >
                    <Trash2 size={15} />
                  </button>
                </div>
              </div>
              {isEditable(selected) ? (
                <textarea
                  value={decodeText(selected.content_base64)}
                  onChange={(event) => updateSelected(event.target.value)}
                  spellCheck={false}
                  className="min-h-[310px] w-full resize-y bg-transparent p-4 font-mono text-xs leading-6 text-slate-200 outline-none"
                />
              ) : (
                <div className="grid min-h-[310px] place-items-center p-8 text-center">
                  <div>
                    <FileText className="mx-auto text-slate-600" size={34} />
                    <p className="mt-4 text-sm text-slate-400">
                      该格式暂不支持在线编辑
                    </p>
                    <button
                      type="button"
                      onClick={downloadSelected}
                      className="mt-4 rounded-xl border border-white/10 px-4 py-2 text-xs text-slate-300"
                    >
                      下载查看
                    </button>
                  </div>
                </div>
              )}
            </>
          ) : (
            <div className="grid min-h-[360px] place-items-center text-center">
              <div>
                <ShieldCheck className="mx-auto text-slate-700" size={36} />
                <p className="mt-4 text-sm text-slate-500">
                  选择左侧文件开始编辑
                </p>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
