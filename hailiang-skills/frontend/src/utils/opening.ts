export const DEFAULT_CLIENT_OPENING =
  "你好！你可以直接告诉我想聊什么。无论你是学生还是家长，我都会根据你的身份和需求来协助你。";

/**
 * The greeting is a presentation concern. It is deliberately not persisted
 * as an assistant message and is never sent to the model as conversation
 * context.
 */
export function buildClientOpeningMessage(recentSummary?: string | null): string {
  const summary = String(recentSummary ?? "").trim();
  return summary
    ? `你好，我们上次聊到了“${summary}”。这次想聊聊什么？`
    : DEFAULT_CLIENT_OPENING;
}
