import { MarkdownContent } from "@/components/MarkdownContent";
import { CitationsBlock } from "@/components/message-blocks/CitationsBlock";
import { FactFormBlock } from "@/components/message-blocks/FactFormBlock";
import { PathActionsBlock } from "@/components/message-blocks/PathActionsBlock";
import { StatusTimelineBlock } from "@/components/message-blocks/StatusTimelineBlock";
import {
  isCitationsBlock,
  isFactFormBlock,
  isPathActionsBlock,
  isStatusTimelineBlock,
  type FactFormField,
  type MessageBlock,
} from "@/types/messageBlocks";
import type { MessageInteractionState } from "@/utils/api";

type MessageBlocksRendererProps = {
  messageId: string;
  blocks: MessageBlock[];
  onPathAction: (pathName: string, description?: string) => void;
  onSubmitFactForm: (
    messageId: string,
    formId: string,
    fields: FactFormField[],
    draftValues: Record<string, unknown>,
  ) => Promise<void>;
  interactionStates?: Record<string, MessageInteractionState>;
};

export function MessageBlocksRenderer({
  messageId,
  blocks,
  onPathAction,
  onSubmitFactForm,
  interactionStates = {},
}: MessageBlocksRendererProps) {
  const groupedBlocks = [
    ...blocks.filter((block) => isCitationsBlock(block)),
    ...blocks.filter((block) => isPathActionsBlock(block)),
    ...blocks.filter((block) => isFactFormBlock(block)),
    ...blocks.filter((block) => isStatusTimelineBlock(block)),
    ...blocks.filter(
      (block) =>
        !isStatusTimelineBlock(block) &&
        !isPathActionsBlock(block) &&
        !isFactFormBlock(block) &&
        !isCitationsBlock(block),
    ),
  ];

  if (!groupedBlocks.length) {
    return null;
  }

  return (
    <div className="space-y-3">
      {groupedBlocks.map((block, index) => {
        const key = `${block.type}-${index}`;
        if (isStatusTimelineBlock(block)) {
          return <StatusTimelineBlock key={key} messageId={messageId} block={block} />;
        }
        if (isFactFormBlock(block)) {
          return (
            <FactFormBlock
              key={key}
              messageId={messageId}
              block={block}
              onSubmit={onSubmitFactForm}
              interactionState={interactionStates[`fact_form:${block.payload.form_id}`]}
            />
          );
        }
        if (isPathActionsBlock(block)) {
          return <PathActionsBlock key={key} block={block} onSelect={onPathAction} interactionState={interactionStates.path_actions} />;
        }
        if (isCitationsBlock(block)) {
          return <CitationsBlock key={key} block={block} />;
        }
        if (block.type === "markdown") {
          const content = typeof block.payload.content === "string" ? block.payload.content : "";
          return (
            <div key={key} className="rounded-2xl border border-white/10 bg-white/[0.03] p-4">
              <MarkdownContent content={content} className="text-slate-100" />
            </div>
          );
        }
        return (
          <div key={key} className="rounded-2xl border border-dashed border-white/10 p-4 text-xs text-slate-400">
            暂未适配的消息块：{block.type}
          </div>
        );
      })}
    </div>
  );
}
