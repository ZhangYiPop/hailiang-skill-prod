import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { submitHarnessV3Answer, resumeHarnessV3 } = vi.hoisted(() => ({
  submitHarnessV3Answer: vi.fn().mockResolvedValue(undefined),
  resumeHarnessV3: vi.fn().mockResolvedValue(undefined),
}));

vi.mock("@/hooks/useHarnessV3Conversation", async () => {
  const { create } = await import("zustand");
  return {
    submitHarnessV3Answer,
    resumeHarnessV3,
    useHarnessV3State: create(() => ({ connection: "idle" })),
  };
});

import { HarnessV3ProgressBlock } from "./HarnessV3ProgressBlock";
import { useHarnessV3State } from "@/hooks/useHarnessV3Conversation";
import type { HarnessV3Frame } from "@/types/harnessV3";

function frame(overrides: Partial<HarnessV3Frame> = {}): HarnessV3Frame {
  return {
    protocol: "hailiang.sse.v3", session_id: "s", run_id: "r", assistant_message_id: "msg_1", expert_team_id: "team_1",
    record_version: 2, cursor: 4, reason: "input_requested", stream_status: "running",
    persistence: { terminal: false },
    state: {
      assistant_message_id: "msg_1", record_version: 2, session_id: "s", profile_id: "p", expert_team_id: "team_1", goal: "规划升学", status: "running",
      phase: "experts_running", action: "专家正在并行分析", completed_count: 1, task_count: 2, waiting_count: 1,
      dispatch: { status: "completed", planned_expert_count: 2, parallel_task_count: 2, experts: [{ node_id: "expert_1", name: "升学专家", brief: "评估升学路径", status: "running" }] },
      nodes: [
        { id: "main", parent_id: null, kind: "main", title: "主编排器", status: "running", action: "等待专家结论" },
        { id: "expert_1", parent_id: "main", kind: "expert", title: "升学分析", actor: "升学专家", status: "completed", result: { conclusion: "建议先明确目标省份", findings: ["分数区间"], limitations: ["省份未知"] } },
      ],
      inputs: [{ id: "input_1", title: "补充成绩", status: "pending", version: 1, round: 1, actor: "升学专家", fields: [
        { key: "score", label: "预估分数", input_type: "integer", required: true, options: [] },
      ] }], result: "", recommendations_status: "not_started", jump_cards: [],
    },
    ...overrides,
  };
}

beforeEach(() => {
  sessionStorage.clear();
  submitHarnessV3Answer.mockClear();
  resumeHarnessV3.mockClear();
  useHarnessV3State.setState({ connection: "idle" });
});
afterEach(() => vi.unstubAllGlobals());

describe("V3 Harness task bubble", () => {
  it("renders dispatch, expert result, and allows a partial answer for a required field", async () => {
    render(<HarnessV3ProgressBlock frame={frame()} />);
    expect(screen.getByText("已拆分为 2 个专家任务 · 可并行 2 个")).toBeInTheDocument();
    expect(screen.getByText("升学专家 的结论")).toBeInTheDocument();
    const number = screen.getByRole("spinbutton", { name: "预估分数" });
    expect(number).not.toHaveAttribute("required");
    fireEvent.click(screen.getByRole("button", { name: "提交已填写内容" }));
    await waitFor(() => expect(submitHarnessV3Answer).toHaveBeenCalledWith(expect.anything(), expect.anything(), {}, false));
  });

  it("offers resume after a timeout without creating a new bubble", async () => {
    useHarnessV3State.setState({ connection: "timeout" });
    const payload = frame({ reason: "stream_timeout", stream_status: "running" });
    render(<HarnessV3ProgressBlock frame={payload} />);
    fireEvent.click(screen.getByRole("button", { name: "继续处理" }));
    await waitFor(() => expect(resumeHarnessV3).toHaveBeenCalledWith(payload));
  });
});
