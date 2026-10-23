import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { HarnessExecutionDiagnostics } from "./HarnessExecutionDiagnostics";

describe("HarnessExecutionDiagnostics", () => {
  it("shows timing and a conservative worker-wait explanation", () => {
    render(<HarnessExecutionDiagnostics diagnostics={{
      total_elapsed_ms: 2400,
      worker_wait_ms: 300,
      first_model_phase: { purpose: "orchestrate_head", duration_ms: 1200 },
      phases: [{ id: "task_1", kind: "expert", title: "升学规划专家", status: "queued", duration_ms: null, queue_reason: "awaiting_worker_claim" }],
    }} />);
    expect(screen.getByLabelText("执行耗时与排队诊断")).toHaveTextContent("流程总耗时：2.40s");
    expect(screen.getByText(/等待 worker 领取/)).toHaveTextContent("可能是 worker 离线或全局执行槽位繁忙");
  });
});
