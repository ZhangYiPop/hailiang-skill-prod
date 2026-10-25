import { describe, expect, it, vi } from "vitest";

import { postSseStream } from "./sse";

function sseResponse(chunks: string[]) {
  const encoder = new TextEncoder();
  return new Response(new ReadableStream({
    start(controller) {
      chunks.forEach((chunk) => controller.enqueue(encoder.encode(chunk)));
      controller.close();
    },
  }), { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

describe("V3 SSE parser", () => {
  it("accepts complete state and terminal frames but ignores ping", async () => {
    const state = { protocol: "hailiang.sse.v3", session_id: "s", run_id: "r", assistant_message_id: "msg_1", expert_team_id: "team", record_version: 1, cursor: 1,
      reason: "stream_started", stream_status: "running", persistence: { terminal: false }, state: { assistant_message_id: "msg_1", record_version: 1, session_id: "s", profile_id: null, expert_team_id: "team", goal: "问题", status: "running", nodes: [], inputs: [], result: "" } };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(sseResponse([
      `event: state\ndata: ${JSON.stringify(state)}\n\n`,
      "event: ping\ndata: {}\n\n",
      `event: done_timeout\ndata: ${JSON.stringify({ ...state, record_version: 2, reason: "stream_timeout", stream_status: "running" })}\n\n`,
    ])));
    const events: string[] = [];
    await postSseStream({ url: "http://test/v3", body: {}, protocol: "hailiang.sse.v3", onEvent: (event) => events.push(event.event) });
    expect(events).toEqual(["state", "done_timeout"]);
  });
});
