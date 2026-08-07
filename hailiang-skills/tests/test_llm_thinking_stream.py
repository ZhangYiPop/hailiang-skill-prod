from __future__ import annotations

import json
import threading
import unittest

import httpx

from hailiang_skills.skill_runtime.llm_client import OpenAICompatibleChatClient
from hailiang_skills.skill_runtime.models import ChatMessage, LLMConfig


class FakeStreamResponse:
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def raise_for_status(self) -> None:
        return None

    def iter_lines(self):
        yield from self.lines

    def close(self) -> None:
        self.closed = True


class FakeHttpClient:
    def __init__(self, response: FakeStreamResponse) -> None:
        self.response = response
        self.requests: list[dict] = []

    def stream(self, method, url, *, content, headers):
        self.requests.append(
            {"method": method, "url": url, "payload": json.loads(content.decode("utf-8")), "headers": headers}
        )
        return self.response


class CapturingLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def log(self, event: str, **payload) -> None:
        self.events.append((event, payload))


class BlockingStreamResponse(FakeStreamResponse):
    def __init__(self) -> None:
        super().__init__([])
        self.started = threading.Event()
        self.closed_event = threading.Event()

    def iter_lines(self):
        self.started.set()
        self.closed_event.wait(timeout=2)
        if self.closed:
            raise httpx.ReadError("stream closed by cancellation")
        return
        yield  # pragma: no cover - makes this a generator for typing

    def close(self) -> None:
        super().close()
        self.closed_event.set()


def make_client() -> OpenAICompatibleChatClient:
    return OpenAICompatibleChatClient(
        LLMConfig(
            provider="dashscope_compatible",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            model="qwen-plus-2025-12-01",
            api_key_env="DASHSCOPE_API_KEY",
            api_key="test-key",
            timeout_s=30,
            temperature=0,
            max_tokens=8000,
            enable_thinking=True,
            return_reasoning=True,
        )
    )


class RuntimeThinkingStreamTest(unittest.TestCase):
    def test_stream_complete_sends_thinking_options_and_parses_reasoning(self) -> None:
        client = make_client()
        response = FakeStreamResponse(
            [
                'data: {"choices":[{"delta":{"reasoning_content":"先想一下"}}]}',
                'data: {"choices":[{"delta":{"content":"正式回答"}}]}',
                "data: [DONE]",
            ]
        )
        fake_http = FakeHttpClient(response)
        client._http = fake_http  # type: ignore[assignment]

        chunks = list(client.stream_complete([ChatMessage(role="user", content="你是谁？")]))

        self.assertTrue(fake_http.requests[0]["payload"]["stream"])
        self.assertTrue(fake_http.requests[0]["payload"]["enable_thinking"])
        self.assertTrue(fake_http.requests[0]["payload"]["return_reasoning"])
        self.assertEqual(chunks[0].reasoning_delta, "先想一下")
        self.assertEqual(chunks[1].content_delta, "正式回答")

    def test_stream_complete_logs_prompt_usage_ttft_and_request_purpose(self) -> None:
        client = make_client()
        response = FakeStreamResponse(
            [
                'data: {"choices":[{"delta":{"content":"回答"}}]}',
                'data: {"choices":[],"usage":{"prompt_tokens":123,"completion_tokens":7,"total_tokens":130}}',
                "data: [DONE]",
            ]
        )
        fake_http = FakeHttpClient(response)
        client._http = fake_http  # type: ignore[assignment]
        logger = CapturingLogger()

        list(
            client.stream_complete(
                [ChatMessage(role="user", content="分析第二轮延迟")],
                logger=logger,  # type: ignore[arg-type]
                request_purpose="main_combined_response",
            )
        )

        self.assertEqual(fake_http.requests[0]["payload"]["stream_options"], {"include_usage": True})
        metrics = next(payload for event, payload in logger.events if event == "llm.request.metrics")
        self.assertEqual(metrics["request_purpose"], "main_combined_response")
        self.assertEqual(metrics["prompt_chars"], len("分析第二轮延迟"))
        self.assertEqual(metrics["input_tokens"], 123)
        self.assertEqual(metrics["output_tokens"], 7)
        self.assertEqual(metrics["total_tokens"], 130)
        self.assertIsNotNone(metrics["ttft_ms"])
        self.assertEqual(metrics["ttft_source"], "first_model_delta")
        self.assertIsNotNone(metrics["content_ttft_ms"])
        self.assertEqual(metrics["content_ttft_source"], "first_content_delta")

    def test_stream_complete_explicitly_disables_provider_default_thinking(self) -> None:
        client = OpenAICompatibleChatClient(
            LLMConfig(
                provider="dashscope_compatible",
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                model="deepseek-v4-flash",
                api_key_env="DASHSCOPE_API_KEY",
                api_key="test-key",
                timeout_s=30,
                temperature=0,
                max_tokens=8000,
                enable_thinking=False,
                return_reasoning=False,
            )
        )
        response = FakeStreamResponse(
            [
                'data: {"choices":[{"delta":{"content":"直接回答"}}]}',
                "data: [DONE]",
            ]
        )
        fake_http = FakeHttpClient(response)
        client._http = fake_http  # type: ignore[assignment]

        list(client.stream_complete([ChatMessage(role="user", content="直接回答")]))

        self.assertIs(fake_http.requests[0]["payload"]["enable_thinking"], False)
        self.assertIs(fake_http.requests[0]["payload"]["return_reasoning"], False)

    def test_content_ttft_is_separate_from_reasoning_ttft(self) -> None:
        client = make_client()
        response = FakeStreamResponse(
            [
                'data: {"choices":[{"delta":{"reasoning_content":"先推理"}}]}',
                'data: {"choices":[{"delta":{"content":"再回答"}}]}',
                'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}',
                "data: [DONE]",
            ]
        )
        fake_http = FakeHttpClient(response)
        client._http = fake_http  # type: ignore[assignment]
        logger = CapturingLogger()

        list(client.stream_complete([ChatMessage(role="user", content="测试")], logger=logger))

        metrics = next(payload for event, payload in logger.events if event == "llm.request.metrics")
        self.assertIsNotNone(metrics["ttft_ms"])
        self.assertIsNotNone(metrics["content_ttft_ms"])
        self.assertGreaterEqual(metrics["content_ttft_ms"], metrics["ttft_ms"])
        self.assertTrue(any(event == "llm.stream.content_ttft" for event, _payload in logger.events))

    def test_cancel_closes_blocked_upstream_stream(self) -> None:
        client = make_client()
        response = BlockingStreamResponse()
        client._http = FakeHttpClient(response)  # type: ignore[assignment]
        cancelled = threading.Event()
        result: list = []

        worker = threading.Thread(
            target=lambda: result.extend(
                client.stream_complete(
                    [ChatMessage(role="user", content="请等待")],
                    cancel_check=cancelled.is_set,
                )
            )
        )
        worker.start()
        self.assertTrue(response.started.wait(timeout=1))
        cancelled.set()
        worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        self.assertTrue(response.closed)
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
