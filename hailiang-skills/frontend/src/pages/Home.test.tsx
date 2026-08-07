import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import Home from "@/pages/Home";
import { useChatStore } from "@/store/useChatStore";

describe("Home", () => {
  beforeEach(() => {
    useChatStore.setState({
      apiBaseUrl: "http://127.0.0.1:8010",
      userId: "debug-user",
      sessionId: "",
      sessionTitle: "",
      messages: [],
      candidatePaths: [],
      events: [],
      userFacts: {},
      sessionFacts: {},
      effectiveFacts: {},
      skillStates: {},
      lastResponse: null,
      composerValue: "",
      formDrafts: {},
      isCreatingSession: false,
      isSending: false,
      isLoadingEvents: false,
      errorMessage: "",
    });
  });

  it("renders chat tester shell", () => {
    render(<Home />);

    expect(screen.getByText("hailiang-skills 对话测试台")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "为当前孩子新建会话" })).toBeInTheDocument();
    expect(screen.getByPlaceholderText("输入你要测试的对话...")).toBeInTheDocument();
  });
});
