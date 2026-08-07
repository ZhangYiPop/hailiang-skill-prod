from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Sequence

from hailiang_skills.skill_runtime.errors import LLMConnectionError, LLMHTTPError, LLMRequestError
from hailiang_skills.core.rate_limit import get_llm_rate_limiter


class EmbeddingClient:
    """Small OpenAI-compatible embeddings client used only for intent routing."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str = "text-embedding-v4",
        timeout_s: int = 8,
        max_batch_size: int = 10,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout_s = timeout_s
        self._max_batch_size = max(int(max_batch_size or 10), 1)
        self.last_batch_count = 0

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def model(self) -> str:
        return self._model

    @property
    def max_batch_size(self) -> int:
        return self._max_batch_size

    def embed(self, inputs: str | Sequence[str]) -> list[list[float]]:
        if isinstance(inputs, str):
            payload_input = str(inputs).strip()
            if not payload_input:
                raise LLMRequestError("embedding input 不能为空")
            self.last_batch_count = 1
            return self._request_embedding(payload_input)

        payload_input = [str(item) for item in inputs if str(item).strip()]
        if not payload_input:
            raise LLMRequestError("embedding input 不能为空")
        vectors: list[list[float]] = []
        batches = list(_chunk_items(payload_input, self._max_batch_size))
        self.last_batch_count = len(batches)
        for batch_index, batch in enumerate(batches, start=1):
            try:
                vectors.extend(self._request_embedding(batch))
            except (LLMConnectionError, LLMHTTPError, LLMRequestError) as exc:
                raise type(exc)(f"{exc} [batch={batch_index}/{len(batches)}]") from exc
        return vectors

    def _request_embedding(self, payload_input: str | list[str]) -> list[list[float]]:
        get_llm_rate_limiter().acquire()
        payload = {
            "model": self._model,
            "input": payload_input,
        }
        request = urllib.request.Request(
            f"{self._base_url}/embeddings",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as response:
                raw_body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="ignore") if exc.fp else str(exc)
            raise LLMHTTPError(f"embedding 调用失败: HTTP {exc.code}: {details}") from exc
        except urllib.error.URLError as exc:
            raise LLMConnectionError(f"embedding 调用失败: {exc.reason}") from exc

        body = json.loads(raw_body)
        data = body.get("data", [])
        vectors: list[list[float]] = []
        if isinstance(data, list):
            for item in data:
                embedding = item.get("embedding") if isinstance(item, dict) else None
                if isinstance(embedding, list):
                    vectors.append([float(value) for value in embedding])
        if not vectors:
            raise LLMRequestError("embedding 响应缺少 data[].embedding")
        return vectors


def _chunk_items(items: Sequence[str], size: int) -> list[list[str]]:
    return [list(items[index : index + size]) for index in range(0, len(items), size)]
