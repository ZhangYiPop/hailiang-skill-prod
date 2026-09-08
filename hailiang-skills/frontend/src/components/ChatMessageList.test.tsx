import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ChatMessageList } from "@/components/ChatMessageList";
import type { ChatMessage } from "@/utils/api";

function makeMessage(message: Partial<ChatMessage> & Pick<ChatMessage, "id" | "role">): ChatMessage {
  return {
    content: "",
    createdAt: "2026-07-06T08:00:00.000Z",
    blocks: [],
    ...message,
  };
}

describe("ChatMessageList", () => {
  it("renders the planning topic badge as a sticky message-stream marker", () => {
    render(
      <ChatMessageList
        messages={[
          makeMessage({ id: "user-1", role: "user", content: "给孩子做规划" }),
          makeMessage({
            id: "assistant-1",
            role: "assistant",
            content: "先了解孩子情况。",
            skillId: "main_planner",
            agentLabel: "升学顾问",
            themeKey: "main-planner",
          }),
          makeMessage({
            id: "assistant-2",
            role: "assistant",
            content: "继续聊同一主题。",
            skillId: "main_planner",
            agentLabel: "升学顾问",
            themeKey: "main-planner",
          }),
          makeMessage({
            id: "assistant-3",
            role: "assistant",
            content: "切换到提分规划。",
            skillId: "score_improve",
            agentLabel: "提分规划",
            themeKey: "score-improve",
          }),
        ]}
      />,
    );

    const badges = screen.getAllByTestId("planning-topic-badge");
    expect(badges).toHaveLength(2);
    expect(badges[0]).toHaveAccessibleName("当前规划主题：升学顾问");
    expect(badges[1]).toHaveAccessibleName("当前规划主题：提分规划");

    const stickyWrapper = badges[0].parentElement;
    expect(stickyWrapper).toHaveClass("sticky");
    expect(stickyWrapper).toHaveClass("top-0");
    expect(badges[0].closest("article")).toBeNull();
    expect(badges[0].closest("section")).not.toBe(badges[1].closest("section"));
  });

  it("renders feedback controls for completed assistant messages", () => {
    render(
      <ChatMessageList
        messages={[
          makeMessage({
            id: "assistant-1",
            messageId: "msg-1",
            role: "assistant",
            content: "这是一条回复。",
            streamingStatus: "completed",
          }),
        ]}
      />,
    );

    expect(screen.getByRole("button", { name: "点赞这条回复" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "点踩这条回复" })).toBeInTheDocument();
  });

  it("does not expose the implicit general-chat skill in the message stream", () => {
    render(
      <ChatMessageList
        messages={[
          makeMessage({ id: "user-1", role: "user", content: "你好" }),
          makeMessage({
            id: "assistant-1",
            role: "assistant",
            content: "你好，有什么可以帮你？",
            skillId: "general_chat",
            agentLabel: "自由问答",
          }),
          makeMessage({
            id: "transition-1",
            role: "assistant",
            messageType: "skill_transition",
            skillTransition: {
              action: "exit",
              from_skill_id: "subject_advisor",
              to_skill_id: "general_chat",
              source: "exit_button",
              created_at: "2026-07-06T08:00:00.000Z",
            },
          }),
        ]}
      />,
    );

    expect(screen.queryByTestId("planning-topic-badge")).not.toBeInTheDocument();
    expect(screen.getByText("已为你退出AI咨询室，如有问题可以继续提问")).toBeInTheDocument();
    expect(screen.queryByText("已退出顾问，进入自由问答")).not.toBeInTheDocument();
  });

  it("renders a historical transition with presentation as a card, not a blank assistant bubble", () => {
    render(
      <ChatMessageList
        messages={[
          makeMessage({
            id: "assistant-route",
            role: "assistant",
            content: "",
            routeSuggestions: [
              { target_skill_id: "career_plan_entity", agent_label: "生涯规划", reason: "继续", confidence: 0.95 },
            ],
          }),
          makeMessage({
            id: "transition-1",
            role: "assistant",
            content: "",
            messageType: "skill_transition",
            skillTransition: {
              action: "enter",
              from_skill_id: "general_chat",
              to_skill_id: "career_plan_entity",
              source: "route_suggestion",
              created_at: "2026-07-06T08:00:00.000Z",
              skill: {
                skill_id: "career_plan_entity",
                name: "升学规划顾问",
                label: "生涯规划",
                info: "进入生涯规划后，我会逐步梳理孩子情况。",
              },
            },
            presentation: {
              assistant: { content: "", status: "completed" },
              intent: {},
              form: {},
              path_options: {},
              skill_rooms: [],
              skill_transition: {},
              session: { active_skill: {} },
              risk: { status: "idle", stage: "", blocked: false, message: "" },
              error: { code: "", message: "", upstream_detail: "", retryable: false, terminal: false },
            },
          }),
        ]}
      />,
    );

    expect(screen.getByText("已进入 生涯规划")).toBeInTheDocument();
    expect(screen.getByText("进入生涯规划后，我会逐步梳理孩子情况。")).toBeInTheDocument();
    expect(screen.getByText("已进入 生涯规划").closest("article")).toBeNull();
  });

  it("renders a confirmed team handoff as a timeline event instead of a user question", () => {
    render(
      <ChatMessageList
        messages={[
          makeMessage({
            id: "handoff-confirmation",
            role: "user",
            content: "@家庭教育专家",
            messageType: "team_handoff_confirmation",
          }),
        ]}
      />,
    );

    const confirmation = screen.getByText("已确认由 家庭教育专家 专家接管");
    expect(confirmation.closest("article")).toBeNull();
    expect(screen.queryByText("你")).not.toBeInTheDocument();
  });

  it("shows the expert and actual execution mode on every expert-team reply", () => {
    render(
      <ChatMessageList
        messages={[
          makeMessage({
            id: "assistant-expert-direct",
            role: "assistant",
            content: "我先帮你梳理情况。",
            presentation: {
              assistant: { content: "我先帮你梳理情况。", status: "completed" },
              intent: {}, form: {}, path_options: {}, skill_rooms: [], skill_transition: {},
              expert: { mode: "team", team: {}, active: { expert_id: "e_career_planner", mention_name: "e生涯规划助手" }, activation: {}, transition: {} },
              session: { active_skill: { skill_id: "expert_direct", title: "e生涯规划助手" } },
              risk: { status: "idle", stage: "", blocked: false, message: "" },
              error: { code: "", message: "", upstream_detail: "", retryable: false, terminal: false },
            },
          }),
          makeMessage({
            id: "assistant-skill",
            role: "assistant",
            content: "下面是提分计划。",
            presentation: {
              assistant: { content: "下面是提分计划。", status: "completed" },
              intent: {}, form: {}, path_options: {}, skill_rooms: [], skill_transition: {},
              expert: { mode: "team", team: {}, active: { expert_id: "academic_coach", mention_name: "学习指导师" }, activation: {}, transition: {} },
              session: { active_skill: { skill_id: "score_improve", title: "提分技能" } },
              risk: { status: "idle", stage: "", blocked: false, message: "" },
              error: { code: "", message: "", upstream_detail: "", retryable: false, terminal: false },
            },
          }),
        ]}
      />,
    );

    expect(screen.getByText("本轮专家：e生涯规划助手")).toBeInTheDocument();
    expect(screen.getByText("执行方式：专家直接回复（未调用独立 Skill）")).toBeInTheDocument();
    expect(screen.getByText("本轮专家：学习指导师")).toBeInTheDocument();
    expect(screen.getByText("本轮 Skill：提分技能")).toBeInTheDocument();
  });

  it("hides historical route suggestions after entering a specialist skill", () => {
    render(
      <ChatMessageList
        activeSkill="subject_advisor"
        messages={[
          makeMessage({
            id: "assistant-old",
            role: "assistant",
            content: "可以继续选择方向。",
            skillId: "general_chat",
            routeSuggestions: [
              {
                target_skill_id: "subject_advisor",
                agent_label: "选科参谋",
                reason: "历史建议",
                confidence: 0.9,
              },
            ],
          }),
          makeMessage({
            id: "assistant-current",
            role: "assistant",
            content: "现在已经进入选科顾问。",
            skillId: "subject_advisor",
          }),
        ]}
      />,
    );

    expect(screen.queryByRole("button", { name: /选科参谋/ })).not.toBeInTheDocument();
  });
});
