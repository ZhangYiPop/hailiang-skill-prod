from __future__ import annotations

import json
import threading
from typing import Any, Iterator
from urllib import error, request

from hailiang_skills.llm.config import LLMConfig
from hailiang_skills.core.rate_limit import get_llm_rate_limiter


def _extract_message_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content", "")
    if isinstance(content, list):
        text_chunks = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_chunks.append(item.get("text", ""))
        return "".join(text_chunks)
    return content if isinstance(content, str) else ""


def _extract_json_block(text: str) -> dict[str, Any]:
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    return json.loads(text)


def _extract_delta_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not choices:
        return ""
    delta = choices[0].get("delta") or {}
    content = delta.get("content", "")
    if isinstance(content, list):
        text_chunks = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_chunks.append(item.get("text", ""))
        return "".join(text_chunks)
    return content if isinstance(content, str) else ""


def _apply_thinking_options(payload: dict[str, Any], enable_thinking: bool, return_reasoning: bool) -> None:
    payload["enable_thinking"] = bool(enable_thinking)
    payload["return_reasoning"] = bool(return_reasoning)


class LLMClientError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, config: LLMConfig) -> None:
        self.config = config

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def chat(self, messages: list[dict[str, str]], **overrides: Any) -> dict[str, Any]:
        if not self.enabled:
            raise LLMClientError(
                f"LLM disabled because env {self.config.api_key_env} is not set."
            )

        get_llm_rate_limiter().acquire()
        url = f"{self.config.base_url}/chat/completions"
        payload = {
            "model": overrides.get("model", self.config.model),
            "messages": messages,
            "temperature": overrides.get("temperature", self.config.temperature),
            "max_tokens": overrides.get("max_tokens", self.config.max_tokens),
        }
        _apply_thinking_options(
            payload,
            bool(overrides.get("enable_thinking", self.config.enable_thinking)),
            bool(overrides.get("return_reasoning", self.config.return_reasoning)),
        )
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.api_key}",
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.config.timeout_s) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise LLMClientError(
                f"LLM HTTP error {exc.code}: {detail or exc.reason}"
            ) from exc
        except error.URLError as exc:
            raise LLMClientError(f"LLM request failed: {exc.reason}") from exc

    def stream_text(
        self, messages: list[dict[str, str]], **overrides: Any
    ) -> Iterator[str]:
        if not self.enabled:
            raise LLMClientError(
                f"LLM disabled because env {self.config.api_key_env} is not set."
            )

        get_llm_rate_limiter().acquire()
        url = f"{self.config.base_url}/chat/completions"
        payload = {
            "model": overrides.get("model", self.config.model),
            "messages": messages,
            "temperature": overrides.get("temperature", self.config.temperature),
            "max_tokens": overrides.get("max_tokens", self.config.max_tokens),
            "stream": True,
        }
        _apply_thinking_options(
            payload,
            bool(overrides.get("enable_thinking", self.config.enable_thinking)),
            bool(overrides.get("return_reasoning", self.config.return_reasoning)),
        )
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.api_key}",
            },
            method="POST",
        )
        cancel_check = overrides.get("cancel_check")
        watcher_stop = threading.Event()
        try:
            with request.urlopen(req, timeout=self.config.timeout_s) as response:
                if callable(cancel_check):
                    def close_on_cancel() -> None:
                        while not watcher_stop.wait(0.05):
                            if cancel_check():
                                try:
                                    response.close()
                                except Exception:
                                    pass
                                return

                    threading.Thread(target=close_on_cancel, daemon=True).start()
                for raw_line in response:
                    if callable(cancel_check) and cancel_check():
                        return
                    line = raw_line.decode("utf-8", errors="ignore").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    chunk = json.loads(data)
                    delta_text = _extract_delta_text(chunk)
                    if delta_text:
                        yield delta_text
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise LLMClientError(
                f"LLM HTTP error {exc.code}: {detail or exc.reason}"
            ) from exc
        except error.URLError as exc:
            if callable(cancel_check) and cancel_check():
                return
            raise LLMClientError(f"LLM request failed: {exc.reason}") from exc
        finally:
            watcher_stop.set()

    def complete_text(
        self, messages: list[dict[str, str]], **overrides: Any
    ) -> str:
        payload = self.chat(messages, **overrides)
        return _extract_message_content(payload)

    def complete_json(
        self, messages: list[dict[str, str]], **overrides: Any
    ) -> dict[str, Any]:
        text = self.complete_text(messages, **overrides)
        try:
            return _extract_json_block(text)
        except json.JSONDecodeError as exc:
            raise LLMClientError(f"LLM JSON decode failed: {text}") from exc
