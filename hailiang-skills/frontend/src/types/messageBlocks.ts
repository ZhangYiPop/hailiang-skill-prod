export type RuntimeStatusItem = {
  stage: string;
  label: string;
  detail?: string;
  source?: "router" | "ms_agent" | "llm" | string;
  seq?: number;
  elapsedMs?: number;
  timestamp?: string;
  status?: "pending" | "active" | "completed" | "failed";
};

export type StatusTimelinePayload = {
  title?: string;
  summary?: string;
  collapsed?: boolean;
  items: RuntimeStatusItem[];
};

export type MarkdownBlock = {
  type: "markdown";
  payload: {
    content: string;
  };
};

export type StatusTimelineBlock = {
  type: "status_timeline";
  payload: StatusTimelinePayload;
};

export type FactFormOption = {
  label: string;
  value: string;
};

export type FactFormField = {
  fact_key: string;
  label: string;
  input_type: "text" | "single_select" | "multi_select" | string;
  required?: boolean;
  placeholder?: string;
  example?: string;
  options?: FactFormOption[];
  submit_mode?: "auto" | "manual" | string;
  scope: "user" | "session" | string;
  value_type?: string;
  max_selections?: number;
};

export type FactFormBlock = {
  type: "fact_form";
  payload: {
    form_id: string;
    title?: string;
    fields: FactFormField[];
  };
};

export type PathActionItem = {
  path_id?: string;
  path_name: string;
  description?: string;
  source?: {
    file?: string;
    record_id?: string;
    sheet?: string;
  };
};

export type PathActionsBlock = {
  type: "path_actions";
  payload: {
    actions: PathActionItem[];
  };
};

export type CitationItem = {
  kind: "fact" | "asset" | string;
  title?: string;
  summary?: string;
  detail?: Record<string, unknown>;
};

export type CitationGroup = {
  kind: "fact" | "asset" | string;
  label?: string;
  items: CitationItem[];
};

export type CitationsBlock = {
  type: "citations";
  payload: {
    groups: CitationGroup[];
  };
};

export type GenericMessageBlock = {
  type: string;
  payload: Record<string, unknown>;
};

export type MessageBlock =
  | StatusTimelineBlock
  | MarkdownBlock
  | FactFormBlock
  | PathActionsBlock
  | CitationsBlock
  | GenericMessageBlock;

export function isFactFormBlock(block: MessageBlock): block is FactFormBlock {
  return block.type === "fact_form";
}

export function isPathActionsBlock(block: MessageBlock): block is PathActionsBlock {
  return block.type === "path_actions";
}

export function isCitationsBlock(block: MessageBlock): block is CitationsBlock {
  return block.type === "citations";
}

export function isStatusTimelineBlock(block: MessageBlock): block is StatusTimelineBlock {
  return block.type === "status_timeline";
}
