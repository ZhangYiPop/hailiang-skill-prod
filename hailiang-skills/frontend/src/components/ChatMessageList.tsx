import { Fragment } from "react";
import { ThumbsDown, ThumbsUp } from "lucide-react";

import type { ChatMessage } from "@/utils/api";
import { MarkdownContent } from "@/components/MarkdownContent";
import { MessageBlocksRenderer } from "@/components/message-blocks/MessageBlocksRenderer";
import { StatusTimelineBlock } from "@/components/message-blocks/StatusTimelineBlock";
import { PathOptionsBlock } from "@/components/message-blocks/PathOptionsBlock";
import { useChatActions } from "@/hooks/useChatActions";
import { isCitationsBlock, isStatusTimelineBlock } from "@/types/messageBlocks";
import type { SseV2PathOptions } from "@/types/streamEvents";

type ChatMessageListProps = {
  messages: ChatMessage[];
  showCitations?: boolean;
  activeSkill?: string;
};

type MessageSection = {
  id: string;
  topicLabel: string;
  messages: ChatMessage[];
};

function formatTime(value: string) {
  return new Date(value).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

function agentLabelForMessage(message: ChatMessage) {
  const presentationSkill = message.presentation?.session.active_skill;
  const presentationSkillId = presentationSkill?.skill_id ?? "";
  // `general_chat` is the implicit conversation fallback.  It is useful for
  // routing and session restoration, but should not create a visible "自由问答"
  // topic marker for the user's first message or after leaving an advisor.
  if (
    presentationSkillId === "general_chat" ||
    message.skillId === "general_chat" ||
    message.skillId === "chat" ||
    message.agentLabel === "自由问答" ||
    message.skillName === "自由问答" ||
    message.sceneName === "自由问答"
  ) {
    return "";
  }
  return presentationSkill?.title || message.agentLabel || message.skillName || message.sceneName || message.skillId || "";
}

function agentKeyForMessage(message: ChatMessage) {
  const label = agentLabelForMessage(message);
  if (!label) {
    return "";
  }
  return [message.presentation?.session.active_skill.skill_id ?? message.skillId ?? "", label, message.themeKey ?? ""].join("|");
}

function PlanningTopicBadge({ label }: { label: string }) {
  return (
    <div className="sticky top-0 z-20 flex justify-center py-2">
      <div
        className="inline-flex max-w-full items-center gap-3 rounded-full border border-[#f4d796]/70 bg-[linear-gradient(135deg,rgba(255,248,218,0.98),rgba(238,198,119,0.96)_46%,rgba(174,124,47,0.98))] px-7 py-2.5 text-sm font-semibold text-[#35230d] shadow-[0_14px_34px_rgba(213,174,92,0.34),inset_0_1px_0_rgba(255,255,255,0.78)]"
        aria-label={`当前规划主题：${label}`}
        data-testid="planning-topic-badge"
      >
        <span className="text-[#73511d]">当前规划主题：</span>
        <span className="truncate tracking-wide text-[#2c1b08]">{label}</span>
      </div>
    </div>
  );
}

function buildMessageSections(messages: ChatMessage[]): MessageSection[] {
  const sections: MessageSection[] = [];
  let currentSection: MessageSection = { id: "initial", topicLabel: "", messages: [] };
  let previousAssistantKey = "";

  messages.forEach((message) => {
    const isAssistant = message.role === "assistant";
    const agentLabel = agentLabelForMessage(message);
    const agentKey = agentKeyForMessage(message);
    const startsNewTopic = isAssistant && Boolean(agentLabel) && agentKey !== previousAssistantKey;

    if (startsNewTopic) {
      if (currentSection.messages.length || currentSection.topicLabel) {
        sections.push(currentSection);
      }
      currentSection = {
        id: `topic-${message.id}`,
        topicLabel: agentLabel,
        messages: [message],
      };
      previousAssistantKey = agentKey;
      return;
    }

    currentSection.messages.push(message);
    if (isAssistant && agentKey) {
      previousAssistantKey = agentKey;
    }
  });

  if (currentSection.messages.length || currentSection.topicLabel) {
    sections.push(currentSection);
  }

  return sections;
}

export function ChatMessageList({ messages, showCitations = false, activeSkill = "" }: ChatMessageListProps) {
  const showUpstreamDetail = import.meta.env.DEV;
  const {
    handlePathAction,
    handleSubmitFactForm,
    handleRouteSuggestion,
    handleConfirmTeamHandoff,
    handleRetryMessage,
    handleMessageFeedback,
  } = useChatActions();

  if (messages.length === 0) {
    return (
      <div className="flex h-full items-center justify-center rounded-[28px] border border-dashed border-white/10 bg-white/[0.03] p-10 text-center text-sm text-slate-400">
        还没有消息。先创建会话，再发送一句测试语句，比如“广东物理类 580 分，看看有哪些路径”。
      </div>
    );
  }

  const sections = buildMessageSections(messages);
  const latestAssistantMessage = [...messages].reverse().find((message) => message.role === "assistant");
  const latestAssistantId = latestAssistantMessage?.id ?? "";
  // Route suggestions are a confirmation affordance for the current
  // general-chat turn only.  Historical cards must remain visible as history,
  // but must not become clickable again after loading a session or entering a
  // specialist Skill.
  const routeSuggestionsEnabled = ["", "general_chat", "career_plan_entity", "main_planner"].includes(activeSkill)
    && (!latestAssistantMessage?.skillId || ["", "general_chat", "career_plan_entity", "main_planner"].includes(latestAssistantMessage.skillId));

  return (
    <div className="space-y-4">
      {sections.map((section) => (
        <section key={section.id} className="relative space-y-4">
          {section.topicLabel ? <PlanningTopicBadge label={section.topicLabel} /> : null}
          {section.messages.map((message) => {
        const isUser = message.role === "user";
        const presentation = message.presentation;
        // Historical transition cards carry a full `presentation` snapshot.
        // They must still render as system cards; otherwise their empty
        // assistant content becomes a blank bubble below the route card.
        const isTransition = message.messageType === "skill_transition";
        const statusBlocks = !isUser
          ? presentation && "steps" in presentation.intent
            ? [{
                type: "status_timeline" as const,
                payload: {
                  title: "推理进度",
                  collapsed: false,
                  items: presentation.intent.steps,
                },
              }]
            : message.blocks.filter((block) => isStatusTimelineBlock(block))
          : [];
        const citationBlocks = showCitations
          ? message.blocks.filter((block) => isCitationsBlock(block))
          : [];
        const otherBlocks = presentation && "form_id" in presentation.form
          ? [{ type: "fact_form" as const, payload: presentation.form }]
          : message.blocks.filter((block) => !isCitationsBlock(block) && !isStatusTimelineBlock(block));
        const teamHandoff = !isUser ? message.teamHandoff : undefined;
        const teamHandoffInteraction = message.interactionStates?.team_handoff;
        const showTeamHandoff = Boolean(
          teamHandoff?.candidates.length
          && (!teamHandoffInteraction?.status || teamHandoffInteraction.status === "active"),
        );
        // Team handoff takes precedence while it is actionable. Once the
        // user confirms or it expires, the recommendation card disappears.
        const skillRooms = !isUser && !showTeamHandoff ? presentation?.skill_rooms ?? [] : [];
        const routeSuggestions = !presentation && !isUser && message.id === latestAssistantId && routeSuggestionsEnabled
          ? message.routeSuggestions ?? []
          : [];
        const selectedRouteSuggestion = message.selectedRouteSuggestion ?? "";
        const routeInteraction = message.interactionStates?.route_suggestions;
        const routeExpired = routeInteraction?.status === "expired";
        const routeSelected = routeInteraction?.status === "selected";
        if (isTransition && message.skillTransition) {
          const transition = message.skillTransition;
          const transitionInfo = transition.skill?.info || transition.skill?.description || "";
          return (
            <div key={message.id} className="flex justify-center">
              <div className="max-w-[82%] rounded-2xl border border-white/10 bg-white/[0.04] px-4 py-3 text-center text-xs text-slate-300">
                <div>
                  {transition.action === "exit"
                    ? "已为你退出AI咨询室，如有问题可以继续提问"
                    : `已进入 ${transition.skill?.label || transition.to_skill_id}`}
                </div>
                {transition.action === "enter" && transitionInfo ? (
                  <div className="mt-2 leading-relaxed text-slate-400">{transitionInfo}</div>
                ) : null}
              </div>
            </div>
          );
        }
        return (
          <Fragment key={message.id}>
            <article
              className={`flex ${isUser ? "justify-end" : "justify-start"}`}
            >
              <div className="max-w-[82%] space-y-3">
                <div
                  className={[
                    "rounded-[24px] border px-4 py-4 shadow-[0_20px_60px_rgba(0,0,0,0.2)]",
                    isUser
                      ? "border-cyan-400/30 bg-cyan-300/10 text-cyan-50"
                      : "border-white/10 bg-slate-950/80 text-slate-100",
                  ].join(" ")}
                >
                  <div className="mb-2 flex items-center gap-2 text-[11px] uppercase tracking-[0.18em] text-slate-400">
                    <span>{isUser ? "你" : "助手"}</span>
                    <span>{formatTime(message.createdAt)}</span>
                    {!isUser && message.streamingStatus === "streaming" ? <span>推理中</span> : null}
                    {!isUser && message.generationStatus === "cancelled" ? <span>已停止</span> : null}
                    {!isUser && message.streamingStatus === "failed" ? <span>失败</span> : null}
                  </div>
                  {!isUser ? (
                    <div className="space-y-3">
                      {statusBlocks.length ? (
                        <div className="space-y-3">
                          {statusBlocks.map((block, index) => (
                            <StatusTimelineBlock
                              key={`${message.id}-status-${index}`}
                              messageId={message.id}
                              block={block}
                            />
                          ))}
                        </div>
                      ) : null}
                      <MessageBlocksRenderer
                        messageId={message.id}
                        blocks={citationBlocks}
                        onPathAction={handlePathAction}
                        onSubmitFactForm={handleSubmitFactForm}
                        interactionStates={message.interactionStates}
                      />
                      {message.content ? (
                        <MarkdownContent content={message.content} className="text-slate-100" />
                      ) : null}
                      {presentation && "options" in presentation.path_options ? (
                        <PathOptionsBlock
                          pathOptions={presentation.path_options as SseV2PathOptions}
                          onSelect={handlePathAction}
                        />
                      ) : null}
                      <MessageBlocksRenderer
                        messageId={message.id}
                        blocks={otherBlocks}
                        onPathAction={handlePathAction}
                        onSubmitFactForm={handleSubmitFactForm}
                        interactionStates={message.interactionStates}
                      />
                      {message.errorMessage ? (
                        <div className="flex flex-col gap-3 rounded-2xl border border-amber-400/20 bg-amber-400/10 px-4 py-3 text-xs text-amber-100 sm:flex-row sm:items-center sm:justify-between">
                          <div className="min-w-0 space-y-2">
                            <span className="leading-relaxed">{message.errorMessage}</span>
                            {showUpstreamDetail && message.presentation?.error.upstream_detail ? (
                              <details className="text-amber-100/80">
                                <summary className="cursor-pointer">本地调试详情</summary>
                                <pre className="mt-2 max-h-32 overflow-auto whitespace-pre-wrap break-all text-[11px]">{message.presentation.error.upstream_detail}</pre>
                              </details>
                            ) : null}
                          </div>
                          {message.streamingStatus === "failed" && message.retryRequest ? (
                            <button
                              type="button"
                              onClick={() => {
                                void handleRetryMessage(message.id);
                              }}
                              className="inline-flex shrink-0 items-center justify-center rounded-full border border-amber-200/40 bg-amber-200/15 px-4 py-2 text-xs font-semibold text-amber-50 transition hover:border-amber-100/70 hover:bg-amber-200/25"
                            >
                              重试
                            </button>
                          ) : null}
                        </div>
                      ) : null}
                      {skillRooms.length ? (
                        <div className="rounded-2xl border border-cyan-300/15 bg-cyan-300/[0.06] px-5 py-4">
                          <p className="text-center text-xs uppercase tracking-[0.2em] text-cyan-100">可进一步选择的规划主题</p>
                          <div className="mt-4 flex flex-wrap justify-center gap-3">
                            {skillRooms.map((room) => {
                              const enabled = Boolean(room.enabled && message.id === latestAssistantId);
                              return (
                                <div key={room.skill_id} className="flex min-w-[160px] flex-col items-center gap-1">
                                  <button
                                    type="button"
                                    disabled={!enabled}
                                    onClick={() => {
                                      void handleRouteSuggestion(message.id, {
                                        target_skill_id: room.skill_id,
                                        agent_label: room.title,
                                        brief: room.brief,
                                        info: room.info,
                                        reason: room.reason || room.description,
                                        confidence: 1,
                                      });
                                    }}
                                    className={[
                                      "w-full rounded-full border px-5 py-2.5 text-sm font-medium transition",
                                      room.status === "entered"
                                        ? "border-cyan-200/70 bg-cyan-200/20 text-cyan-50"
                                        : "border-white/10 bg-white/[0.06] text-slate-200 hover:border-cyan-300/35 hover:bg-cyan-300/10 hover:text-cyan-50",
                                      !enabled ? "cursor-not-allowed opacity-40" : "",
                                    ].join(" ")}
                                  >
                                    {room.status === "entered" ? "已进入：" : ""}
                                    {room.title}
                                  </button>
                                  {room.brief ? <span className="text-center text-[11px] leading-relaxed text-slate-400">{room.brief}</span> : null}
                                </div>
                              );
                            })}
                          </div>
                        </div>
                      ) : showTeamHandoff && teamHandoff ? (
                        <div className="rounded-2xl border border-violet-300/20 bg-violet-300/[0.07] px-5 py-4">
                          <p className="text-center text-xs uppercase tracking-[0.2em] text-violet-100">建议由以下专家接管</p>
                          {teamHandoff.reason ? <p className="mt-2 text-center text-xs leading-relaxed text-slate-300">{teamHandoff.reason}</p> : null}
                          <div className="mt-4 flex flex-wrap justify-center gap-3">
                            {teamHandoff.candidates.map((candidate) => {
                              const selected = teamHandoffInteraction?.status === "selected" && teamHandoffInteraction.selected_target_skill_id === candidate.expert_id;
                              const locked =
                                !message.messageId ||
                                teamHandoffInteraction?.status === "selected" ||
                                teamHandoffInteraction?.status === "expired";
                              return (
                                <div key={candidate.expert_id} className="flex min-w-[160px] flex-col items-center gap-1">
                                  <button
                                    type="button"
                                    disabled={Boolean(locked)}
                                    onClick={() => void handleConfirmTeamHandoff(message.id, candidate.expert_id, candidate.mention_name)}
                                    className={[
                                      "w-full rounded-full border px-5 py-2.5 text-sm font-medium transition",
                                      selected ? "border-violet-200/70 bg-violet-200/20 text-violet-50" : "border-white/10 bg-white/[0.06] text-slate-200 hover:border-violet-300/40 hover:bg-violet-300/10 hover:text-violet-50",
                                      locked ? "cursor-not-allowed opacity-40" : "",
                                    ].join(" ")}
                                  >
                                    {selected ? "已转交：" : "@"}{candidate.mention_name}
                                  </button>
                                  {candidate.brief ? <span className="text-center text-[11px] leading-relaxed text-slate-400">{candidate.brief}</span> : null}
                                </div>
                              );
                            })}
                          </div>
                          {teamHandoffInteraction?.status === "expired" ? <p className="mt-3 text-center text-xs text-slate-400">该转交建议已失效，请以最新对话为准。</p> : null}
                        </div>
                      ) : routeSuggestions.length ? (
                        <div className="rounded-2xl border border-cyan-300/15 bg-cyan-300/[0.06] px-5 py-4">
                          <p className="text-center text-xs uppercase tracking-[0.2em] text-cyan-100">
                            可进一步选择的规划主题
                          </p>
                          {routeExpired ? <p className="mt-2 text-center text-xs text-slate-400">已失效，请以最新回复为准。</p> : null}
                          <div className="mt-4 flex flex-wrap justify-center gap-3">
                            {routeSuggestions.map((suggestion) => {
                              const selected = routeSelected
                                ? routeInteraction?.selected_target_skill_id === suggestion.target_skill_id
                                : selectedRouteSuggestion === suggestion.target_skill_id;
                              const locked = routeExpired || routeSelected || Boolean(selectedRouteSuggestion);
                              return (
                                  <div key={suggestion.target_skill_id} className="flex min-w-[160px] flex-col items-center gap-1">
                                    <button
                                      type="button"
                                      disabled={locked}
                                      onClick={() => {
                                        void handleRouteSuggestion(message.id, suggestion);
                                      }}
                                      className={[
                                        "w-full rounded-full border px-5 py-2.5 text-sm font-medium transition",
                                        selected
                                          ? "border-cyan-200/70 bg-cyan-200/20 text-cyan-50 shadow-[0_0_24px_rgba(103,232,249,0.18)]"
                                          : "border-white/10 bg-white/[0.06] text-slate-200 hover:border-cyan-300/35 hover:bg-cyan-300/10 hover:text-cyan-50",
                                        locked ? "cursor-not-allowed opacity-40" : "",
                                      ].join(" ")}
                                      title={suggestion.reason}
                                    >
                                      {selected ? "已选择：" : ""}
                                      {suggestion.agent_label || suggestion.target_skill_id}
                                    </button>
                                    {suggestion.brief ? <span className="text-center text-[11px] leading-relaxed text-slate-400">{suggestion.brief}</span> : null}
                                  </div>
                              );
                            })}
                          </div>
                          {selectedRouteSuggestion ? (
                            <p className="mt-3 text-center text-xs text-slate-400">
                              已单选一个 agent，其他选项已锁定。
                            </p>
                          ) : null}
                        </div>
                      ) : null}
                      {message.streamingStatus === "completed" && message.messageId && !isTransition ? (
                        <div className="flex items-center justify-end gap-2 border-t border-white/5 pt-3">
                          <span className="mr-1 text-xs text-slate-500">这条回复对你有帮助吗？</span>
                          <button
                            type="button"
                            aria-label="点赞这条回复"
                            title="点赞"
                            onClick={() => {
                              void handleMessageFeedback(message.id, "like");
                            }}
                            className={`rounded-full border p-2 transition ${
                              message.feedback === "like"
                                ? "border-emerald-300/60 bg-emerald-300/15 text-emerald-200"
                                : "border-white/10 text-slate-400 hover:border-emerald-300/40 hover:text-emerald-200"
                            }`}
                          >
                            <ThumbsUp size={15} aria-hidden="true" />
                          </button>
                          <button
                            type="button"
                            aria-label="点踩这条回复"
                            title="点踩"
                            onClick={() => {
                              void handleMessageFeedback(message.id, "dislike");
                            }}
                            className={`rounded-full border p-2 transition ${
                              message.feedback === "dislike"
                                ? "border-amber-300/60 bg-amber-300/15 text-amber-200"
                                : "border-white/10 text-slate-400 hover:border-amber-300/40 hover:text-amber-200"
                            }`}
                          >
                            <ThumbsDown size={15} aria-hidden="true" />
                          </button>
                        </div>
                      ) : null}
                    </div>
                  ) : (
                    <MarkdownContent content={message.content} className="text-cyan-50" />
                  )}
                </div>
              </div>
            </article>
          </Fragment>
        );
          })}
        </section>
      ))}
    </div>
  );
}
