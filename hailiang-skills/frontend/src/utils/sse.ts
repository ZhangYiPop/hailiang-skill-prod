import type { StreamEvent } from "@/types/streamEvents";
import type { HarnessV3Event, HarnessV3EventName } from "@/types/harnessV3";

type StreamOptionsBase = {
  url: string;
  body: unknown;
  signal?: AbortSignal;
  retryCount?: number;
  retryDelayMs?: number;
  retryableStatus?: number[];
  onRetry?: (attempt: number, error: Error) => void;
};

type V2StreamOptions = StreamOptionsBase & {
  protocol?: "hailiang.sse.v2";
  onEvent: (event: StreamEvent) => void;
};

type V3StreamOptions = StreamOptionsBase & {
  protocol: "hailiang.sse.v3";
  onEvent: (event: HarnessV3Event) => void;
};

type StreamOptions = V2StreamOptions | V3StreamOptions;

const DEFAULT_STREAM_RETRY_COUNT = 3;
const DEFAULT_STREAM_RETRY_DELAY_MS = 600;
const DEFAULT_STREAM_RETRYABLE_STATUS = [408, 429, 500, 502, 503, 504];

function parseChunk(rawEvent: string): Array<StreamEvent | HarnessV3Event> {
  const lines = rawEvent.replace(/\r\n/g, "\n").split("\n");
  let eventName = "message";
  const dataLines: string[] = [];
  for (const line of lines) {
    if (line.startsWith("event:")) {
      eventName = line.slice(6).trim();
      continue;
    }
    if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trim());
    }
  }
  if (!dataLines.length) {
    return [];
  }
  const rawData = dataLines.join("\n");
  try {
    const data = JSON.parse(rawData) as Record<string, unknown>;
    if (eventName === "state") {
      if (data.protocol === "hailiang.sse.v2") {
        return [{ event: "state", data: data as StreamEvent["data"] }];
      }
      if (data.protocol === "hailiang.sse.v3") {
        return [{ event: "state", data: data as HarnessV3Event["data"] }];
      }
      return [];
    }
    if (
      ["done_suspended", "done_timeout", "done"].includes(eventName)
      && data.protocol === "hailiang.sse.v3"
      && typeof data.assistant_message_id === "string"
      && typeof data.record_version === "number"
    ) {
      return [{ event: eventName as HarnessV3EventName, data: data as HarnessV3Event["data"] }];
    }
    if (
      eventName === "done"
      && data.protocol === "hailiang.sse.v2"
      && typeof data.session_id === "string"
      && typeof data.run_id === "string"
    ) {
      return [{ event: "done", data: data as StreamEvent["data"] }];
    }
    return [];
  } catch {
    return [{
      event: eventName,
      data: { raw: rawData },
    }];
  }
}


function wait(ms: number) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function toError(error: unknown): Error {
  if (error instanceof Error) {
    return error;
  }
  return new Error(String(error || "流式请求异常"));
}

function wrapRetryError(error: Error, retryCount: number): Error {
  return new Error(`流式请求异常，已自动重试 ${retryCount} 次：${error.message}`);
}

export function postSseStream(options: V2StreamOptions): Promise<void>;
export function postSseStream(options: V3StreamOptions): Promise<void>;
export async function postSseStream({
  url,
  body,
  onEvent,
  signal,
  retryCount = DEFAULT_STREAM_RETRY_COUNT,
  retryDelayMs = DEFAULT_STREAM_RETRY_DELAY_MS,
  retryableStatus = DEFAULT_STREAM_RETRYABLE_STATUS,
  onRetry,
  protocol = "hailiang.sse.v2",
}: StreamOptions): Promise<void> {
  let lastError: Error | null = null;

  for (let attempt = 0; attempt <= retryCount; attempt += 1) {
    let receivedAnyEvent = false;
    let retryableFailure = true;
    try {
      const headers = new Headers({
        "Content-Type": "application/json",
        Accept: "text/event-stream",
        "X-SSE-Protocol": protocol,
      });
      const response = await fetch(url, {
        method: "POST",
        headers,
        body: JSON.stringify(body),
        signal,
      });

      if (!response.ok) {
        const message = (await response.text()) || `请求失败: ${response.status}`;
        const error = new Error(message);
        retryableFailure = retryableStatus.includes(response.status);
        if (!retryableFailure || attempt === retryCount) {
          throw error;
        }
        lastError = error;
      } else {
        if (!response.body) {
          throw new Error("浏览器不支持流式响应");
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let buffer = "";

        while (true) {
          const { done, value } = await reader.read();
          buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done });
          // SSE permits CRLF or LF line endings. Normalize before framing so
          // a proxy using CRLF cannot turn a complete V3 snapshot into a
          // never-flushed buffer.
          buffer = buffer.replace(/\r\n/g, "\n");
          const parts = buffer.split("\n\n");
          buffer = parts.pop() ?? "";
          for (const part of parts) {
            const events = parseChunk(part.trim());
            for (const event of events) {
              receivedAnyEvent = true;
              onEvent(event as never);
            }
          }
          if (done) {
            break;
          }
        }

        if (buffer.trim()) {
          const events = parseChunk(buffer.trim());
          for (const event of events) {
            receivedAnyEvent = true;
            onEvent(event as never);
          }
        }

        if (!receivedAnyEvent) {
          throw new Error("流式连接已关闭但未返回内容");
        }
        return;
      }
    } catch (error) {
      if (signal?.aborted || !retryableFailure) {
        throw toError(error);
      }
      const normalizedError = toError(error);
      if (receivedAnyEvent || attempt === retryCount) {
        throw attempt === retryCount ? wrapRetryError(normalizedError, retryCount) : normalizedError;
      }
      lastError = normalizedError;
    }

    if (attempt < retryCount && lastError) {
      onRetry?.(attempt + 1, lastError);
      await wait(retryDelayMs * (attempt + 1));
    }
  }

  throw wrapRetryError(lastError ?? new Error("流式请求异常"), retryCount);
}
