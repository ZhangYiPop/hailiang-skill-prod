import { fireEvent, render, screen, waitFor, within, cleanup } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TaskProgressBlock } from "./TaskProgressBlock";
import { refreshHarness, useHarnessState } from "@/hooks/useHarnessConversation";
import { useChatStore } from "@/store/useChatStore";
import type { HarnessRun } from "@/types/harness";

function fixture(): HarnessRun {
  return { run_id: "run1", session_id: "s", profile_id: "p", root_task_id: "root", goal: "共同分析", status: "running", version: 3,
    created_at: "2026-10-09T00:00:00Z", updated_at: "2026-10-09T00:00:01Z", result: "", completed_count: 0, task_count: 2, waiting_count: 2,
    nodes: [
      { id: "root", parent_id: null, kind: "main", title: "主任务", actor: "主 agent", status: "waiting_for_dependency", started_at: null, ended_at: null },
      { id: "a", parent_id: "root", kind: "expert", title: "任务 A", actor: "专家 A", status: "waiting_for_user", started_at: null, ended_at: null },
      { id: "b", parent_id: "root", kind: "expert", title: "任务 B", actor: "专家 B", status: "waiting_for_user", started_at: null, ended_at: null },
    ], inputs: [
      { id: "q1", title: "A 的问题", status: "pending", version: 1, round: 2, fields: [{ key: "grade", label: "年级", input_type: "text", required: true, options: [] }], bindings: [{ task_id: "a", tool_id: "a:call1" }] },
      { id: "q2", title: "B 的问题", status: "pending", version: 1, round: 1, fields: [{ key: "subject", label: "科目", input_type: "single_select", required: true, options: [{ label: "数学", value: "math" }] }], bindings: [{ task_id: "b", tool_id: "b:call2" }] },
    ] };
}

beforeEach(() => {
  sessionStorage.clear();
  useHarnessState.getState().set({ key: "", activeRunId: "", runs: {}, cursor: 0, connection: "connected" });
  useChatStore.setState({ apiBaseUrl: "http://test", sessionId: "s", userId: "u", selectedProfileId: "p", activeProfileId: "p", messages: [], isSending: false });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("Harness task bubble", () => {
  it("submits filled fields without requiring the rest, and discards drafts on full skip", async () => {
    const run = fixture();
    run.inputs[0].fields.push({ key: "province", label: "省份", input_type: "text", required: true, options: [] });
    const submit = vi.fn().mockResolvedValue(undefined);
    render(<TaskProgressBlock run={run} onSubmitAnswer={submit} />);
    const form = screen.getByRole("form", { name: "A 的问题" });
    expect(within(form).getByRole("textbox", { name: "省份" })).not.toBeRequired();
    fireEvent.change(within(form).getByRole("textbox", { name: "年级" }), { target: { value: "高二" } });
    fireEvent.click(within(form).getByRole("button", { name: "提交并继续" }));
    await waitFor(() => expect(submit).toHaveBeenCalledTimes(1));
    expect(submit.mock.calls[0][1]).toEqual({ grade: "高二" });
    await waitFor(() => expect(within(form).getByRole("button", { name: "全部跳过" })).not.toBeDisabled());
    fireEvent.click(within(form).getByRole("button", { name: "全部跳过" }));
    await waitFor(() => expect(submit).toHaveBeenCalledTimes(2));
    expect(submit.mock.calls[1][1]).toEqual({});
    expect(submit.mock.calls[1][2]).toBe(true);
  });
  it("keeps expert cards accessible when task details are collapsed and submits independently", async () => {
    const submit = vi.fn().mockResolvedValue(undefined);
    render(<TaskProgressBlock run={fixture()} onSubmitAnswer={submit} />);
    fireEvent.click(screen.getByText("主任务"));
    const a = screen.getByRole("form", { name: "A 的问题" });
    const b = screen.getByRole("form", { name: "B 的问题" });
    fireEvent.change(within(a).getByRole("textbox", { name: "年级" }), { target: { value: "高二" } });
    fireEvent.click(within(a).getByRole("button", { name: "提交并继续" }));
    await waitFor(() => expect(submit).toHaveBeenCalledTimes(1));
    expect(submit.mock.calls[0][0].id).toBe("q1");
    expect(submit.mock.calls[0][1]).toEqual({ grade: "高二" });
    fireEvent.click(within(b).getByRole("button", { name: "全部跳过" }));
    await waitFor(() => expect(submit).toHaveBeenCalledTimes(2));
    expect(submit.mock.calls[1][0].id).toBe("q2");
    expect(submit.mock.calls[1][2]).toBe(true);
  });

  it("preserves drafts and submission IDs after a transport failure", async () => {
    const submit = vi.fn().mockRejectedValue(new Error("网络断开"));
    const { unmount } = render(<TaskProgressBlock run={fixture()} onSubmitAnswer={submit} />);
    const a = screen.getByRole("form", { name: "A 的问题" });
    fireEvent.change(within(a).getByRole("textbox"), { target: { value: "高二" } });
    fireEvent.click(within(a).getByRole("button", { name: "提交并继续" }));
    await screen.findByRole("alert");
    const submission = submit.mock.calls[0][3];
    unmount();
    render(<TaskProgressBlock run={fixture()} onSubmitAnswer={submit} />);
    const restored = screen.getByRole("form", { name: "A 的问题" });
    expect(within(restored).getByRole("textbox")).toHaveValue("高二");
    fireEvent.click(within(restored).getByRole("button", { name: "提交并继续" }));
    await waitFor(() => expect(submit).toHaveBeenCalledTimes(2));
    expect(submit.mock.calls[1][3]).toBe(submission);
  });

  it("retains the newer snapshot and a single work bubble when older responses arrive", async () => {
    const run = fixture();
    const fetch = vi.fn().mockResolvedValueOnce({ ok: true, json: async () => ({ runtime_mode: "harness_v1", cursor: 12, runs: [{ ...run, version: 5, status: "completed", result: "主 agent 结论" }] }) })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ runtime_mode: "harness_v1", cursor: 10, runs: [run] }) });
    vi.stubGlobal("fetch", fetch);
    await refreshHarness();
    await refreshHarness();
    expect(useHarnessState.getState().runs.run1.version).toBe(5);
    expect(useHarnessState.getState().cursor).toBe(12);
    expect(useHarnessState.getState().activeRunId).toBe("");
    expect(useChatStore.getState().messages.filter((m) => m.id === "harness-work-run1")).toHaveLength(1);
    expect(useChatStore.getState().isSending).toBe(false);
  });

  it("shows disconnection as connection state without changing task status", () => {
    useHarnessState.getState().set({ connection: "reconnecting" });
    render(<TaskProgressBlock run={fixture()} />);
    expect(screen.getByRole("status")).toHaveTextContent("后台任务继续执行");
    expect(screen.queryByText("执行失败")).toBeNull();
  });
});
