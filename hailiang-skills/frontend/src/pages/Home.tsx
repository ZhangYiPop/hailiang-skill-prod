import { useCallback, useEffect, useMemo, useRef } from "react";

import {
  Bot,
  Cable,
  DatabaseZap,
  LayoutPanelLeft,
  MessagesSquare,
  MoonStar,
  SunMedium,
} from "lucide-react";

import { ChatMessageList } from "@/components/ChatMessageList";
import { Composer } from "@/components/Composer";
import { DebugIdentityPanel } from "@/components/DebugIdentityPanel";
import { EventPanel } from "@/components/EventPanel";
import { FactsManagerPanel } from "@/components/FactsManagerPanel";
import { getAutoDetectedApiBaseUrl } from "@/config/runtime";
import { ProfileSwitcher } from "@/components/ProfileSwitcher";
import { SessionSidebar } from "@/components/SessionSidebar";
import { StatusPill } from "@/components/StatusPill";
import { SummaryPanel } from "@/components/SummaryPanel";
import { useChatActions } from "@/hooks/useChatActions";
import { useChatStore } from "@/store/useChatStore";

export default function Home() {
  const hasBootstrappedRef = useRef(false);
  const conversationScrollRef = useRef<HTMLDivElement | null>(null);
  const autoScrollEnabledRef = useRef(true);
  const isProgrammaticScrollRef = useRef(false);
  const previousMessageCountRef = useRef(0);
  const {
    apiBaseUrl,
    debugIdentity,
    userId,
    currentScenario,
    viewMode,
    themeMode,
    enableThinking,
    profiles,
    activeProfileId,
    activeProfileName,
    sessionList,
    sessionId,
    sessionTitle,
    activeSkill,
    skillCatalog,
    expertCatalog,
    expertTeamCatalog,
    activeExpertId,
    activeExpertTeam,
    messages,
    candidatePaths,
    sharedFacts,
    profileFacts,
    sessionFacts,
    effectiveFacts,
    skillStates,
    events,
    lastResponse,
    isCreatingSession,
    isLoadingEvents,
    isLoadingProfiles,
    isLoadingSessions,
    isSwitchingSession,
    isSending,
    isCancellingRun,
    errorMessage,
    setApiBaseUrl,
    resetApiBaseUrl,
    setViewMode,
    setThemeMode,
    setEnableThinking,
  } = useChatStore();
  const {
    applyDebugIdentity,
    bootstrapDebugIdentity,
    resetDebugIdentity,
    selectProfile,
    loadSession,
    handleCreateProfile,
    handleCreateSession,
    handleDeleteSession,
    handleRenameSession,
    handleSendMessage,
    handleSelectExpert,
    handleExitExpert,
    handleSelectExpertTeam,
    handleStopGeneration,
    handleRefreshEvents,
    handleDownloadSessionLogs,
    handleClearUserFactsBySource,
  } = useChatActions();
  const autoDetectedApiBaseUrl = getAutoDetectedApiBaseUrl();
  const isChatMode = viewMode === "chat";
  const showQuickPrompts = messages.every((message) => message.role !== "user");
  const scenarioLabel = useMemo(() => {
    const scenarioMap: Record<string, string> = {
      admission_simulation: "模拟升学",
      multi_path_planning: "多元路径规划",
      profile_building: "学生画像构建",
      subject_selection: "选科与专业问询",
      interest_plan: "兴趣培养与行动计划",
    };
    return scenarioMap[currentScenario] ?? currentScenario ?? "";
  }, [currentScenario]);
  const activeSkillLabel = useMemo(
    () => skillCatalog.find((skill) => skill.skill_id === activeSkill)?.label || activeSkill,
    [activeSkill, skillCatalog],
  );
  const activeExpert = useMemo(
    () => expertCatalog.find((expert) => expert.expert_id === activeExpertId) ?? null,
    [activeExpertId, expertCatalog],
  );

  useEffect(() => {
    if (hasBootstrappedRef.current) {
      return;
    }
    hasBootstrappedRef.current = true;
    void bootstrapDebugIdentity();
  }, [bootstrapDebugIdentity]);

  useEffect(() => {
    document.body.dataset.theme = themeMode;
    document.documentElement.dataset.theme = themeMode;
    return () => {
      delete document.body.dataset.theme;
      delete document.documentElement.dataset.theme;
    };
  }, [themeMode]);

  const scrollConversationToBottom = useCallback(() => {
    const container = conversationScrollRef.current;
    if (!container) {
      return;
    }
    isProgrammaticScrollRef.current = true;
    container.scrollTop = container.scrollHeight;
    window.setTimeout(() => {
      isProgrammaticScrollRef.current = false;
    }, 0);
  }, []);

  const handleConversationScroll = useCallback(() => {
    if (isProgrammaticScrollRef.current) {
      return;
    }
    const container = conversationScrollRef.current;
    if (!container) {
      return;
    }
    const distanceFromBottom = container.scrollHeight - container.scrollTop - container.clientHeight;
    autoScrollEnabledRef.current = distanceFromBottom < 80;
  }, []);

  const latestMessage = messages[messages.length - 1];
  const latestMessageSignature = latestMessage
    ? [
        latestMessage.id,
        latestMessage.content.length,
        latestMessage.blocks.length,
        latestMessage.reasoningContent?.length ?? 0,
        latestMessage.streamingStatus ?? "",
      ].join(":")
    : "";

  useEffect(() => {
    const previousMessageCount = previousMessageCountRef.current;
    const appendedMessage = messages.length > previousMessageCount;
    const userMessageAppended = appendedMessage
      ? messages.slice(previousMessageCount).some((message) => message.role === "user")
      : false;
    if (userMessageAppended) {
      autoScrollEnabledRef.current = true;
    }
    previousMessageCountRef.current = messages.length;
    if (!autoScrollEnabledRef.current) {
      return;
    }
    window.requestAnimationFrame(scrollConversationToBottom);
  }, [latestMessage?.role, latestMessageSignature, messages.length, scrollConversationToBottom]);

  const connectionControlCard = (
    <div className="rounded-[28px] border border-white/10 bg-white/[0.04] p-5">
      <div className="mb-5 flex items-center gap-3">
        <Cable size={16} className="text-cyan-200" />
        <div>
          <p className="text-xs uppercase tracking-[0.22em] text-slate-500">连接控制</p>
          <h2 className="mt-1 text-lg font-semibold text-white">会话与接口配置</h2>
        </div>
      </div>

      <div className="grid gap-4 md:grid-cols-[1.6fr_auto]">
        <label className="block">
          <span className="mb-2 block text-xs uppercase tracking-[0.18em] text-slate-500">
            API Base URL
          </span>
          <input
            value={apiBaseUrl}
            onChange={(event) => setApiBaseUrl(event.target.value)}
            className="w-full rounded-2xl border border-white/10 bg-slate-950/80 px-4 py-3 text-sm text-white outline-none transition focus:border-cyan-300/40"
          />
        </label>
        <div className="grid gap-4 md:grid-cols-2">
          <div className="flex items-end">
            <button
              type="button"
              onClick={resetApiBaseUrl}
              className="w-full rounded-2xl border border-white/10 bg-white/[0.05] px-5 py-3 text-sm font-medium text-slate-100 transition hover:border-cyan-300/40 hover:bg-cyan-300/10"
            >
              自动识别
            </button>
          </div>
          <button
            type="button"
            onClick={() => {
              void handleCreateSession();
            }}
            disabled={isCreatingSession || !activeProfileId}
            className="w-full rounded-2xl border border-cyan-300/40 bg-cyan-300/15 px-5 py-3 text-sm font-medium text-cyan-50 transition hover:bg-cyan-300/25 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {isCreatingSession ? "创建中..." : "为当前孩子新建会话"}
          </button>
        </div>
      </div>

      <p className="mt-3 text-xs leading-6 text-slate-400">
        默认会按当前页面地址自动推导后端接口，例如当前会识别为
        <span className="mx-1 text-cyan-200">{autoDetectedApiBaseUrl}</span>
        。如果部署脚本注入了运行时配置，也会优先使用注入值。
      </p>

      <div className="mt-4 flex flex-wrap items-center gap-3">
        <StatusPill label={sessionId ? `会话 ${sessionId}` : "未创建会话"} />
        <StatusPill label={userId ? `用户 ${userId}` : "未登录"} />
        <StatusPill label={activeProfileName ? `孩子 ${activeProfileName}` : "未选择孩子"} />
        <StatusPill
          label={activeExpertTeam ? `专家团 ${activeExpertTeam.name} · ${activeExpertTeam.active_mention_name}` : activeExpert ? `专家 ${activeExpert.name}` : "未选择专家"}
          tone={activeExpertTeam || activeExpert ? "info" : "default"}
        />
        <StatusPill
          label={enableThinking ? "Thinking 已开启" : "Thinking 已关闭"}
          tone={enableThinking ? "info" : "default"}
        />
        <StatusPill
          label={errorMessage ? "请求异常" : "接口正常待测"}
          tone={errorMessage ? "warning" : "success"}
        />
      </div>

      <div className="mt-4 grid gap-3">
        <label className="flex items-center justify-between gap-4 rounded-2xl border border-white/10 bg-slate-950/60 px-4 py-3">
          <span>
            <span className="block text-sm font-medium text-slate-100">Thinking 模式</span>
            <span className="mt-1 block text-xs leading-5 text-slate-500">
              开启后本轮请求同时启用 enable_thinking 与 return_reasoning，并在消息卡片展示 Thinking
            </span>
          </span>
          <input
            type="checkbox"
            checked={enableThinking}
            onChange={(event) => setEnableThinking(event.target.checked)}
            className="h-5 w-5 accent-cyan-300"
          />
        </label>
      </div>

      {errorMessage ? (
        <div className="mt-4 rounded-2xl border border-amber-400/20 bg-amber-400/10 px-4 py-3 text-sm text-amber-100">
          {errorMessage}
        </div>
      ) : null}
    </div>
  );

  const factsCard = (
    <FactsManagerPanel
      apiBaseUrl={apiBaseUrl}
      userId={userId}
      profileId={activeProfileId}
      profileName={activeProfileName}
      sessionId={sessionId}
      sharedFacts={sharedFacts}
      profileFacts={profileFacts}
      sessionFacts={sessionFacts}
      onSaved={handleRefreshEvents}
      onCleared={handleClearUserFactsBySource}
    />
  );

  const conversationCard = (
    <div className="rounded-[28px] border border-white/10 bg-white/[0.04] p-5">
      <div className="mb-5">
        <p className="text-xs uppercase tracking-[0.22em] text-slate-500">对话区域</p>
        <h2 className="mt-1 text-lg font-semibold text-white">消息往返</h2>
      </div>
      <div className="grid gap-5">
        <div
          ref={conversationScrollRef}
          onScroll={handleConversationScroll}
          className="max-h-[620px] overflow-y-auto pr-2"
        >
          <ChatMessageList messages={messages} activeSkill={activeSkill} showCitations={!isChatMode} />
        </div>
        <Composer
          disabled={!sessionId}
          showQuickPrompts={showQuickPrompts}
          onSubmit={handleSendMessage}
          expertCatalog={expertCatalog}
          expertTeamCatalog={expertTeamCatalog}
          activeExpertId={activeExpertId}
          activeExpertTeam={activeExpertTeam}
          onSelectExpert={handleSelectExpert}
          onExitExpert={handleExitExpert}
          onSelectExpertTeam={handleSelectExpertTeam}
          onExitExpertTeam={() => handleSelectExpertTeam("")}
          isGenerating={isSending}
          isCancelling={isCancellingRun}
          onStopGeneration={handleStopGeneration}
        />
      </div>
    </div>
  );

  const debugIdentityCard = (
    <DebugIdentityPanel
      identity={debugIdentity}
      onApply={applyDebugIdentity}
      onReset={resetDebugIdentity}
    />
  );

  const profileSwitcherCard = (
    <ProfileSwitcher
      profiles={profiles}
      activeProfileId={activeProfileId}
      loading={isLoadingProfiles}
      onSelect={(profileId) => {
        void selectProfile(profileId);
      }}
      onCreate={(input) => {
        void handleCreateProfile(input);
      }}
    />
  );

  const headerControlsCard = (
    <section className="rounded-[24px] border border-white/10 bg-white/[0.04] p-4">
      <div className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="min-w-[120px]">
            <p className="text-[11px] uppercase tracking-[0.18em] text-slate-500">展示模式</p>
            <p className="mt-1 text-sm font-medium text-white">控制页面信息密度</p>
          </div>
          <div className="flex items-center gap-2 rounded-full border border-white/10 bg-slate-950/70 p-1">
            {[
              { value: "chat", label: "对话模式" },
              { value: "debug", label: "调试模式" },
            ].map((mode) => (
              <button
                key={mode.value}
                type="button"
                onClick={() => setViewMode(mode.value as "chat" | "debug")}
                className={[
                  "rounded-full px-3 py-2 text-xs transition",
                  viewMode === mode.value
                    ? "bg-cyan-300/20 text-cyan-50"
                    : "text-slate-400 hover:text-slate-200",
                ].join(" ")}
              >
                {mode.label}
              </button>
            ))}
          </div>
        </div>

        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="min-w-[120px]">
            <p className="text-[11px] uppercase tracking-[0.18em] text-slate-500">主题模式</p>
            <p className="mt-1 text-sm font-medium text-white">切换白天 / 夜间主题</p>
          </div>
          <div className="flex items-center gap-2 rounded-full border border-white/10 bg-slate-950/70 p-1">
            {[
              { value: "dark", label: "夜间", icon: MoonStar },
              { value: "light", label: "白天", icon: SunMedium },
            ].map((mode) => {
              const Icon = mode.icon;
              return (
                <button
                  key={mode.value}
                  type="button"
                  onClick={() => setThemeMode(mode.value as "dark" | "light")}
                  className={[
                    "inline-flex items-center gap-2 rounded-full px-3 py-2 text-xs transition",
                    themeMode === mode.value
                      ? "bg-cyan-300/20 text-cyan-50"
                      : "text-slate-400 hover:text-slate-200",
                  ].join(" ")}
                >
                  <Icon size={14} />
                  {mode.label}
                </button>
              );
            })}
          </div>
        </div>

        <p className="text-xs leading-5 text-slate-400">
          注释：对话模式突出聊天主体；调试模式显示调试卡片。白天模式适合长时间阅读。
        </p>
      </div>
    </section>
  );

  return (
    <main className="app-shell min-h-screen bg-[#07111f] text-white">
      <div className="mx-auto max-w-[1920px] px-3 py-4 sm:px-4 lg:px-5">
        <section className="relative overflow-hidden rounded-[36px] border border-white/10 bg-[radial-gradient(circle_at_top_left,_rgba(34,211,238,0.18),_transparent_28%),radial-gradient(circle_at_top_right,_rgba(245,158,11,0.12),_transparent_24%),linear-gradient(180deg,_rgba(10,18,34,0.94),_rgba(4,10,20,0.98))] shadow-[0_30px_120px_rgba(0,0,0,0.45)]">
          <div className="absolute inset-0 bg-[linear-gradient(rgba(255,255,255,0.03)_1px,transparent_1px),linear-gradient(90deg,rgba(255,255,255,0.03)_1px,transparent_1px)] bg-[size:24px_24px] opacity-40" />
          <div className="relative border-b border-white/10 px-6 py-5 lg:px-8">
            <div className="flex flex-col gap-5 lg:flex-row lg:items-start lg:justify-between">
              <div className="max-w-3xl">
                <div className="mb-4 flex flex-wrap items-center gap-2">
                  <StatusPill label="对话测试前端" tone="success" />
                  <StatusPill label={sessionId ? "会话已建立" : "等待创建会话"} />
                  <StatusPill
                    label={activeExpertTeam ? `专家团：${activeExpertTeam.name}` : activeExpert ? `已进入 ${activeExpert.name}` : "未选择专家"}
                    tone={activeExpertTeam || activeExpert ? "info" : "default"}
                  />
                </div>
                <h1 className="text-3xl font-semibold tracking-tight text-white sm:text-4xl">
                  hailiang-skills 对话测试台
                </h1>
                <p className="mt-4 max-w-2xl text-sm leading-7 text-slate-300 sm:text-base">
                  用一个页面同时观察消息往返、Skill 命中、候选路径、事实快照与事件流。
                  这个界面专门为你当前的升学规划 Agent 调试而做。
                </p>
              </div>

              <div className="w-full max-w-[560px] space-y-3">
                {headerControlsCard}
                {!isChatMode ? (
                  <div className="grid gap-3 sm:grid-cols-4">
                    <div className="rounded-2xl border border-white/10 bg-white/[0.04] px-4 py-3">
                      <div className="flex items-center gap-3">
                        <MessagesSquare size={16} className="text-cyan-200" />
                        <span className="text-xs uppercase tracking-[0.18em] text-slate-400">消息数</span>
                      </div>
                      <p className="mt-3 text-2xl font-semibold text-white">{messages.length}</p>
                    </div>
                    <div className="rounded-2xl border border-white/10 bg-white/[0.04] px-4 py-3">
                      <div className="flex items-center gap-3">
                        <Bot size={16} className="text-cyan-200" />
                        <span className="text-xs uppercase tracking-[0.18em] text-slate-400">Active Skill</span>
                      </div>
                      <p className="mt-3 truncate text-lg font-semibold text-white">
                        {activeSkillLabel || "--"}
                      </p>
                    </div>
                    <div className="rounded-2xl border border-white/10 bg-white/[0.04] px-4 py-3">
                      <div className="flex items-center gap-3">
                        <LayoutPanelLeft size={16} className="text-cyan-200" />
                        <span className="text-xs uppercase tracking-[0.18em] text-slate-400">Scenario</span>
                      </div>
                      <p className="mt-3 truncate text-lg font-semibold text-white">
                        {scenarioLabel || "--"}
                      </p>
                    </div>
                    <div className="rounded-2xl border border-white/10 bg-white/[0.04] px-4 py-3">
                      <div className="flex items-center gap-3">
                        <DatabaseZap size={16} className="text-cyan-200" />
                        <span className="text-xs uppercase tracking-[0.18em] text-slate-400">事件数</span>
                      </div>
                      <p className="mt-3 text-2xl font-semibold text-white">{events.length}</p>
                    </div>
                  </div>
                ) : null}
              </div>
            </div>
          </div>

          <div
            className={[
              "relative grid gap-5 px-4 py-5 lg:px-5",
              isChatMode
                ? "lg:grid-cols-[280px_minmax(0,1.55fr)_minmax(360px,0.9fr)]"
                : "lg:grid-cols-[280px_minmax(0,1.2fr)_minmax(420px,0.95fr)]",
            ].join(" ")}
          >
            <aside className="space-y-6">
              {isChatMode ? null : debugIdentityCard}
              {isChatMode ? null : profileSwitcherCard}
              <SessionSidebar
                sessions={sessionList}
                activeSessionId={sessionId}
                activeProfileName={activeProfileName}
                loading={isLoadingProfiles || isLoadingSessions || isSwitchingSession}
                onSelect={(nextSessionId) => {
                  void loadSession(nextSessionId);
                }}
                onRename={async (targetSessionId, title) => {
                  await handleRenameSession(targetSessionId, title);
                }}
                onDelete={async (targetSessionId) => {
                  await handleDeleteSession(targetSessionId);
                }}
              />
            </aside>

            <section className="space-y-6">
              {isChatMode ? conversationCard : connectionControlCard}
              {isChatMode ? null : factsCard}
              {isChatMode ? null : conversationCard}
            </section>

            {isChatMode ? (
              <aside className="space-y-6">
                {debugIdentityCard}
                {profileSwitcherCard}
                {connectionControlCard}
                {factsCard}
              </aside>
            ) : viewMode === "debug" ? (
              <aside className="space-y-6">
                <SummaryPanel
                  lastResponse={lastResponse}
                  activeSkill={activeSkill}
                  candidatePaths={candidatePaths}
                  userId={userId}
                  profileName={activeProfileName}
                  sessionTitle={sessionTitle}
                  sharedFacts={sharedFacts}
                  profileFacts={profileFacts}
                  sessionFacts={sessionFacts}
                  effectiveFacts={effectiveFacts}
                  skillStates={skillStates}
                />
                <EventPanel
                  events={events}
                  loading={isLoadingEvents}
                  onRefresh={handleRefreshEvents}
                  onDownloadLogs={handleDownloadSessionLogs}
                  canDownloadLogs={Boolean(sessionId)}
                />
              </aside>
            ) : null}
          </div>
        </section>
      </div>
    </main>
  );
}
