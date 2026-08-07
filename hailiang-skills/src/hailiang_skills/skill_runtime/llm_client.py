from __future__ import annotations

import json
import os
import threading
from time import perf_counter
from typing import Callable, Iterator, Sequence

import httpx

from hailiang_skills.skill_runtime.errors import (
    LLMConnectionError,
    LLMHTTPError,
    LLMRequestError,
    LLMResponseFormatError,
    MissingAPIKeyError,
)
from hailiang_skills.skill_runtime.models import (
    AssistantTurnResult,
    ChatMessage,
    ChatStreamChunk,
    LLMConfig,
    ToolCallRequest,
    ToolSpec,
)
from hailiang_skills.skill_runtime.runtime_logger import RuntimeLogger, preview_text
from hailiang_skills.core.telemetry import span, text_fingerprint
from hailiang_skills.core.audit import audit_text
from hailiang_skills.core.rate_limit import get_llm_rate_limiter


class OpenAICompatibleChatClient:
    def __init__(self, config: LLMConfig) -> None:
        if not config.api_key:
            raise MissingAPIKeyError(f"缺少环境变量 {config.api_key_env}，无法调用模型接口")
        self._config = config
        self._http = httpx.Client(
            timeout=httpx.Timeout(config.timeout_s, connect=min(10, config.timeout_s)),
            limits=httpx.Limits(
                max_connections=int(os.getenv("HAILIANG_LLM_MAX_CONNECTIONS", "100")),
                max_keepalive_connections=int(os.getenv("HAILIANG_LLM_MAX_KEEPALIVE", "20")),
            ),
        )
        self._request_metrics = threading.local()

    def last_request_metrics(self) -> dict[str, object]:
        return dict(getattr(self._request_metrics, "value", {}) or {})

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        logger: RuntimeLogger | None = None,
        request_purpose: str = "unspecified",
    ) -> str:
        result = self.complete_with_tools(
            messages,
            (),
            preferred_mode="none",
            logger=logger,
            request_purpose=request_purpose,
        )
        return result.final_text

    def complete_with_tools(
        self,
        messages: Sequence[ChatMessage],
        tool_specs: Sequence[ToolSpec],
        *,
        preferred_mode: str = "native",
        logger: RuntimeLogger | None = None,
        request_purpose: str = "unspecified",
    ) -> AssistantTurnResult:
        if not messages:
            raise LLMRequestError("模型请求消息不能为空")

        if preferred_mode == "none":
            raw_body = self._post_chat_completions(
                messages,
                tool_specs=(),
                tool_mode="none",
                logger=logger,
                request_purpose=request_purpose,
            )
            if logger:
                logger.log("llm.response.none", raw_body_preview=preview_text(raw_body, limit=800))
            return AssistantTurnResult(final_text=_extract_message_text(raw_body), tool_mode="none")

        try:
            if preferred_mode == "native":
                raw_body = self._post_chat_completions(
                    messages,
                    tool_specs=tool_specs,
                    tool_mode="native",
                    logger=logger,
                    request_purpose=request_purpose,
                )
                if logger:
                    logger.log("llm.response.native", raw_body_preview=preview_text(raw_body, limit=800))
                native_result = _extract_tool_aware_result(raw_body)
                if native_result.tool_calls or native_result.final_text:
                    if logger:
                        logger.log(
                            "llm.turn_result.native",
                            final_text_preview=preview_text(native_result.final_text),
                            tool_calls=[
                                {"id": item.id, "name": item.name, "arguments": item.arguments}
                                for item in native_result.tool_calls
                            ],
                        )
                    return native_result
        except (LLMHTTPError, LLMResponseFormatError, LLMConnectionError) as exc:
            if logger:
                logger.log("llm.native.failed", error=str(exc))
            if preferred_mode != "native":
                raise

        raw_body = self._post_chat_completions(
            messages,
            tool_specs=(),
            tool_mode="json_action",
            logger=logger,
            request_purpose=request_purpose,
        )
        if logger:
            logger.log("llm.response.json_action", raw_body_preview=preview_text(raw_body, limit=800))
        result = _extract_json_action_result(raw_body)
        if logger:
            logger.log(
                "llm.turn_result.json_action",
                final_text_preview=preview_text(result.final_text),
                tool_calls=[
                    {"id": item.id, "name": item.name, "arguments": item.arguments}
                    for item in result.tool_calls
                ],
            )
        return result

    def _post_chat_completions(
        self,
        messages: Sequence[ChatMessage],
        *,
        tool_specs: Sequence[ToolSpec],
        tool_mode: str,
        logger: RuntimeLogger | None = None,
        request_purpose: str = "unspecified",
    ) -> str:
        get_llm_rate_limiter().acquire()
        endpoint = f"{self._config.base_url}/chat/completions"
        payload = _build_chat_payload(
            self._config.model,
            self._config.temperature,
            self._config.max_tokens,
            messages,
            tool_specs=tool_specs,
            tool_mode=tool_mode,
        )
        _apply_thinking_options(payload, self._config.enable_thinking, self._config.return_reasoning)
        prompt_chars = _prompt_chars(messages)
        audit_record = audit_text("llm_request", json.dumps(payload, ensure_ascii=False))
        if logger:
            logger.log(
                "llm.request",
                request_purpose=request_purpose,
                tool_mode=tool_mode,
                endpoint=endpoint,
                message_count=len(messages),
                prompt_chars=prompt_chars,
                enabled_tools=[item.name for item in tool_specs if item.enabled],
                payload_fingerprint=text_fingerprint(json.dumps(payload, ensure_ascii=False), preview_chars=0),
                audit=audit_record,
            )
        body = json.dumps(payload).encode("utf-8")
        headers = {
                "Authorization": f"Bearer {self._config.api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
        }
        try:
            started = perf_counter()
            with span("llm.chat_completions", node="llm_request", attributes={"provider": "openai_compatible", "model": self._config.model, "mode": tool_mode}):
                response = self._http.post(endpoint, content=body, headers=headers)
                response.raise_for_status()
                raw_response = response.text
                usage = _extract_usage(raw_response)
                metrics = {
                    "request_purpose": request_purpose,
                    "stream": False,
                    "model": self._config.model,
                    "prompt_chars": prompt_chars,
                    "input_tokens": usage.get("input_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                    "total_tokens": usage.get("total_tokens"),
                    "ttft_ms": None,
                    "ttft_source": "unavailable_non_stream",
                    "duration_ms": round((perf_counter() - started) * 1000, 3),
                }
                self._request_metrics.value = metrics
                if logger:
                    logger.log("llm.response.audit", audit=audit_text("llm_response", raw_response))
                    logger.log("llm.request.metrics", **metrics)
                return raw_response
        except httpx.HTTPStatusError as exc:
            details = _extract_http_error_details(exc.response)
            raise LLMHTTPError(f"模型调用失败: HTTP {exc.response.status_code}: {details}") from exc
        except httpx.HTTPError as exc:
            raise LLMConnectionError(f"模型调用失败: {exc}") from exc

    def stream_complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        logger: RuntimeLogger | None = None,
        cancel_check: Callable[[], bool] | None = None,
        request_purpose: str = "unspecified",
    ) -> Iterator[ChatStreamChunk]:
        if not messages:
            raise LLMRequestError("模型请求消息不能为空")

        get_llm_rate_limiter().acquire()
        endpoint = f"{self._config.base_url}/chat/completions"
        payload = _build_chat_payload(
            self._config.model,
            self._config.temperature,
            self._config.max_tokens,
            messages,
            tool_specs=(),
            tool_mode="none",
        )
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        _apply_thinking_options(payload, self._config.enable_thinking, self._config.return_reasoning)
        prompt_chars = _prompt_chars(messages)
        audit_record = audit_text("llm_stream_request", json.dumps(payload, ensure_ascii=False))
        if logger:
            logger.log(
                "llm.request.stream",
                request_purpose=request_purpose,
                tool_mode="none",
                endpoint=endpoint,
                message_count=len(messages),
                prompt_chars=prompt_chars,
                payload_fingerprint=text_fingerprint(json.dumps(payload, ensure_ascii=False), preview_chars=0),
                audit=audit_record,
            )
        body = json.dumps(payload).encode("utf-8")
        headers = {
                "Authorization": f"Bearer {self._config.api_key}",
                "Accept": "text/event-stream",
                "Content-Type": "application/json",
        }
        if cancel_check and cancel_check():
            return
        try:
            started = perf_counter()
            first_delta = True
            ttft_ms: float | None = None
            content_ttft_ms: float | None = None
            usage: dict[str, int | None] = {}
            with span("llm.chat_completions.stream", node="llm_stream", attributes={"provider": "openai_compatible", "model": self._config.model}):
                with self._http.stream("POST", endpoint, content=body, headers=headers) as response:
                    response.raise_for_status()
                    watcher_stop = threading.Event()
                    if cancel_check:
                        def close_on_cancel() -> None:
                            while not watcher_stop.wait(0.05):
                                if cancel_check():
                                    # `iter_lines()` may be blocked waiting for
                                    # a token. Closing the response interrupts
                                    # that read and closes the upstream stream.
                                    try:
                                        response.close()
                                    except Exception:
                                        pass
                                    return

                        threading.Thread(target=close_on_cancel, daemon=True).start()
                    try:
                        for raw_line in response.iter_lines():
                            if cancel_check and cancel_check():
                                return
                            line = raw_line.strip()
                            if not line or not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data)
                            except json.JSONDecodeError:
                                if logger:
                                    logger.log("llm.stream.invalid_json", line_fingerprint=text_fingerprint(line, preview_chars=0))
                                continue
                            chunk_usage = _extract_usage_payload(chunk)
                            if chunk_usage:
                                usage = chunk_usage
                            stream_chunk = _extract_stream_chunk(chunk)
                            if stream_chunk.content_delta or stream_chunk.reasoning_delta:
                                if first_delta:
                                    ttft_ms = round((perf_counter() - started) * 1000, 3)
                                    if logger:
                                        logger.log(
                                            "llm.stream.ttft",
                                            request_purpose=request_purpose,
                                            prompt_chars=prompt_chars,
                                            duration_ms=ttft_ms,
                                        )
                                first_delta = False
                                if stream_chunk.content_delta and content_ttft_ms is None:
                                    content_ttft_ms = round((perf_counter() - started) * 1000, 3)
                                    if logger:
                                        logger.log(
                                            "llm.stream.content_ttft",
                                            request_purpose=request_purpose,
                                            prompt_chars=prompt_chars,
                                            duration_ms=content_ttft_ms,
                                        )
                                yield stream_chunk
                    finally:
                        watcher_stop.set()
                        metrics = {
                            "request_purpose": request_purpose,
                            "stream": True,
                            "model": self._config.model,
                            "prompt_chars": prompt_chars,
                            "input_tokens": usage.get("input_tokens"),
                            "output_tokens": usage.get("output_tokens"),
                            "total_tokens": usage.get("total_tokens"),
                            "ttft_ms": ttft_ms,
                            "ttft_source": "first_model_delta" if ttft_ms is not None else "no_model_delta",
                            "content_ttft_ms": content_ttft_ms,
                            "content_ttft_source": (
                                "first_content_delta" if content_ttft_ms is not None else "no_content_delta"
                            ),
                            "duration_ms": round((perf_counter() - started) * 1000, 3),
                        }
                        self._request_metrics.value = metrics
                        if logger:
                            logger.log("llm.request.metrics", **metrics)
        except httpx.HTTPStatusError as exc:
            details = _extract_http_error_details(exc.response)
            raise LLMHTTPError(f"模型流式调用失败: HTTP {exc.response.status_code}: {details}") from exc
        except httpx.HTTPError as exc:
            if cancel_check and cancel_check():
                return
            raise LLMConnectionError(f"模型流式调用失败: {exc}") from exc


def _build_chat_payload(
    model: str,
    temperature: float,
    max_tokens: int,
    messages: Sequence[ChatMessage],
    *,
    tool_specs: Sequence[ToolSpec],
    tool_mode: str,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": [_message_to_payload(item) for item in messages],
    }
    enabled_specs = [item for item in tool_specs if item.enabled]
    if tool_mode == "native" and enabled_specs:
        payload["tools"] = [_tool_spec_to_native_payload(item) for item in enabled_specs]
        payload["tool_choice"] = "auto"
    return payload


def _apply_thinking_options(payload: dict[str, object], enable_thinking: bool, return_reasoning: bool) -> None:
    # Some compatible providers enable reasoning by default when these fields
    # are omitted. Send the configured booleans explicitly so `false` is an
    # actual runtime instruction rather than a local-only default.
    payload["enable_thinking"] = bool(enable_thinking)
    payload["return_reasoning"] = bool(return_reasoning)


def _prompt_chars(messages: Sequence[ChatMessage]) -> int:
    return sum(len(str(message.content or "")) for message in messages)


def _extract_usage(raw_body: str) -> dict[str, int | None]:
    try:
        payload = json.loads(raw_body)
    except (TypeError, json.JSONDecodeError):
        return {}
    return _extract_usage_payload(payload)


def _extract_usage_payload(payload: object) -> dict[str, int | None]:
    if not isinstance(payload, dict):
        return {}
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return {}

    def token_value(*keys: str) -> int | None:
        for key in keys:
            value = usage.get(key)
            if isinstance(value, int):
                return value
        return None

    return {
        "input_tokens": token_value("prompt_tokens", "input_tokens"),
        "output_tokens": token_value("completion_tokens", "output_tokens"),
        "total_tokens": token_value("total_tokens"),
    }


def _message_to_payload(message: ChatMessage) -> dict[str, object]:
    payload: dict[str, object] = {
        "role": message.role,
        "content": message.content,
    }
    if message.tool_call_id:
        payload["tool_call_id"] = message.tool_call_id
    if message.name:
        payload["name"] = message.name
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": item.id,
                "type": "function",
                "function": {
                    "name": item.name,
                    "arguments": json.dumps(item.arguments, ensure_ascii=False),
                },
            }
            for item in message.tool_calls
        ]
    return payload


def _tool_spec_to_native_payload(spec: ToolSpec) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters_schema,
        },
    }


def _extract_http_error_details(response: httpx.Response) -> str:
    raw_body = response.text.strip()
    if not raw_body:
        return "响应体为空"

    try:
        data = json.loads(raw_body)
    except json.JSONDecodeError:
        return raw_body

    error = data.get("error")
    if isinstance(error, dict):
        message = str(error.get("message") or "").strip()
        error_type = str(error.get("type") or "").strip()
        error_code = str(error.get("code") or "").strip()
        details = [part for part in [message, error_type, error_code] if part]
        if details:
            return " | ".join(details)
    return raw_body


def _extract_message_text(raw_body: str) -> str:
    return _extract_tool_aware_result(raw_body).final_text


def _extract_stream_chunk(payload: dict[str, object]) -> ChatStreamChunk:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ChatStreamChunk()
    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        return ChatStreamChunk()
    delta = first_choice.get("delta")
    message = first_choice.get("message")
    if not isinstance(delta, dict) and not isinstance(message, dict):
        return ChatStreamChunk()
    delta = delta if isinstance(delta, dict) else {}
    message = message if isinstance(message, dict) else {}
    content_payload = delta.get("content")
    reasoning_payload = (
        delta.get("reasoning_content") or delta.get("reasoning") or delta.get("reasoning_text")
    )
    if content_payload is None:
        content_payload = message.get("content")
    if reasoning_payload is None:
        reasoning_payload = (
            message.get("reasoning_content")
            or message.get("reasoning")
            or message.get("reasoning_text")
        )
    return ChatStreamChunk(
        content_delta=_extract_delta_text(content_payload),
        reasoning_delta=_extract_delta_text(reasoning_payload),
    )


def _extract_delta_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
        return "".join(parts)
    return ""


def _extract_tool_aware_result(raw_body: str) -> AssistantTurnResult:
    try:
        data = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise LLMResponseFormatError(f"模型返回不是合法 JSON: {exc.msg}") from exc

    if not isinstance(data, dict):
        raise LLMResponseFormatError("模型返回顶层必须是 JSON 对象")

    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMResponseFormatError(f"模型返回缺少 choices: {raw_body}")

    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        raise LLMResponseFormatError(f"模型返回首个 choice 不是对象: {raw_body}")

    message = first_choice.get("message")
    if not isinstance(message, dict):
        raise LLMResponseFormatError(f"模型返回缺少 message: {raw_body}")

    tool_calls_payload = message.get("tool_calls")
    if isinstance(tool_calls_payload, list) and tool_calls_payload:
        tool_calls = _parse_tool_calls(tool_calls_payload)
        return AssistantTurnResult(tool_mode="native", tool_calls=tuple(tool_calls))

    content = message.get("content")
    if isinstance(content, str):
        return AssistantTurnResult(final_text=content.strip(), tool_mode="native")
    if isinstance(content, list):
        text = _join_content_parts(content)
        if text:
            return AssistantTurnResult(final_text=text, tool_mode="native")
        raise LLMResponseFormatError("模型返回 content 为数组，但未包含可用文本片段")
    if content is None:
        refusal = message.get("refusal")
        if isinstance(refusal, str) and refusal.strip():
            raise LLMResponseFormatError(f"模型拒绝返回内容: {refusal.strip()}")
    raise LLMResponseFormatError(f"模型返回 content 类型不支持: {type(content).__name__}")


def _join_content_parts(content: list[object]) -> str:
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts).strip()


def _parse_tool_calls(tool_calls_payload: list[object]) -> list[ToolCallRequest]:
    tool_calls: list[ToolCallRequest] = []
    for item in tool_calls_payload:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "function":
            continue
        function_payload = item.get("function")
        if not isinstance(function_payload, dict):
            continue
        name = function_payload.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        arguments_payload = function_payload.get("arguments")
        arguments = _parse_tool_arguments(arguments_payload)
        tool_calls.append(
            ToolCallRequest(
                id=str(item.get("id") or f"call_{len(tool_calls)+1}"),
                name=name.strip(),
                arguments=arguments,
            )
        )
    if not tool_calls:
        raise LLMResponseFormatError("模型返回了 tool_calls，但未包含可解析的 function 调用。")
    return tool_calls


def _parse_tool_arguments(arguments_payload: object) -> dict[str, object]:
    if arguments_payload is None:
        return {}
    if isinstance(arguments_payload, dict):
        return arguments_payload
    if isinstance(arguments_payload, str):
        stripped = arguments_payload.strip()
        if not stripped:
            return {}
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise LLMResponseFormatError(f"tool arguments 不是合法 JSON: {exc.msg}") from exc
        if not isinstance(parsed, dict):
            raise LLMResponseFormatError("tool arguments 顶层必须是 JSON 对象")
        return parsed
    raise LLMResponseFormatError(f"tool arguments 类型不支持: {type(arguments_payload).__name__}")


def _extract_json_action_result(raw_body: str) -> AssistantTurnResult:
    text = _extract_tool_aware_result(raw_body).final_text
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return AssistantTurnResult(final_text=text, tool_mode="json_action")

    if not isinstance(payload, dict):
        return AssistantTurnResult(final_text=text, tool_mode="json_action")
    action = str(payload.get("action") or "").strip()
    if action == "call_tool":
        tool_name = str(payload.get("tool_name") or "").strip()
        if not tool_name:
            raise LLMResponseFormatError("JSON action 缺少 tool_name")
        arguments = payload.get("arguments", {})
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise LLMResponseFormatError("JSON action 的 arguments 必须是对象")
        return AssistantTurnResult(
            tool_mode="json_action",
            tool_calls=(ToolCallRequest(id="json_action_call", name=tool_name, arguments=arguments),),
        )
    if action == "final":
        response = payload.get("response")
        if not isinstance(response, str):
            raise LLMResponseFormatError("JSON action 的 response 必须是字符串")
        return AssistantTurnResult(final_text=response.strip(), tool_mode="json_action")
    return AssistantTurnResult(final_text=text, tool_mode="json_action")
